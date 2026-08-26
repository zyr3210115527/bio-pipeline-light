#!/usr/bin/env python3
"""从交付包的 knowledge_card.yaml + WDL 生成 skill/references/knowledge_cards_map.json。

交付方每次给的是一整包 `<工具>/knowledge_card.yaml`（外加同目录 WDL）。手抄进 map 抄一次
错一次——0824 交付就漏了 10 张 bulk10 卡的 meta_xlsx/clinical_xls/clinical_csv/output_base，
以及 7 个工具整张卡。这个脚本把「交付包 -> map」变成可重跑的一步。

    python3 scripts/build_knowledge_cards.py /path/to/解压后的交付包 [--write]

不带 --write 只打印差异。

收录口径（map 是给 validate_execution_chain 做契约校验用的，不是卡片全文镜像）：
  - inputs：expose_level=basic 的参数，并入当前 map 里已有的同名参数（只增不减，
    保证以前能过校验的计划现在照样过）。advanced/infra 全是有默认值的调参旋钮，不收。
  - outputs：expose_level=primary，并入当前已有的。
  - require_any：卡片 interface.validators 里的 one_of，服务端据此做「二选一」检查。
  - reference_resource：容器内自带默认值的参考资源（索引/参考基因组/依赖包），
    缺了不算缺必填。以前是服务端一个硬编码 set，只覆盖 5 个原子工具；现在卡片自带。
类型以 WDL 声明为准（File / File? / Array[File]+ …），卡片 yaml 的 type 只有小写标量，
表达不了可选和数组。
"""
import argparse
import json
import os
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("需要 PyYAML：pip3 install pyyaml")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(HERE, "skill", "references", "knowledge_cards_map.json")
CATALOG_PATH = os.path.join(HERE, "skill", "references", "tool_catalog.csv")

# 参数名后缀 -> 格式。比 artifact_type 准：meta_xlsx 和 sample_csv 的 artifact_type
# 都是 sample_metadata，格式却一个 XLSX 一个 CSV。
SUFFIX_FORMAT = [
    ("_xlsx", "XLSX"), ("_xls", "XLS"), ("_csv", "CSV"), ("_tsv", "TSV"),
    ("_rds", "RDS"), ("_h5ad", "H5AD"), ("_zip", "ZIP"), ("_json", "JSON"),
    ("_maf", "MAF"), ("_bed", "BED"), ("_gtf", "GTF"), ("_gmt", "GMT"),
]
ARTIFACT_FORMAT = {
    "fastq": "FASTQ.gz", "bam": "BAM", "bam_index": "BAI", "ubam": "BAM", "cram": "CRAM",
    "cram_index": "CRAI", "vcf": "VCF.gz", "vcf_index": "TBI", "reference_vcf": "VCF.gz",
    "expression_matrix": "TSV", "count_matrix": "TSV", "differential_expression": "TSV",
    "sample_metadata": "CSV", "individual_metadata": "CSV", "clinical_table": "CSV",
    "cell_metadata": "CSV", "gtf": "GTF", "reference_fasta": "FASTA",
    "reference_fai": "FAI", "reference_dict": "DICT", "bed": "BED",
    "genomic_intervals": "BED", "interval_list": "INTERVAL_LIST",
    "star_index": "TAR.GZ", "rsem_index": "TAR.GZ", "dependency_archive": "ZIP",
    "seurat_rds": "RDS", "gene_list": "TXT", "gene_set": "GMT", "pedigree": "PED",
}
# 交付包里 primary 输出用了 110 多种 artifact_type（plot_pdf / qc_table / joint_vcf …），
# 一一列举没意义，按词根归格式。输出格式只用于展示，输入格式才进数据探查。
ARTIFACT_PATTERNS = [
    (r"(^|_)(pdf|plot_pdf)s?($|_)", "PDF"), (r"(^|_)png s?|plot_png", "PNG"),
    (r"(^|_)svg", "SVG"), (r"html", "HTML"), (r"json", "JSON"),
    (r"(^|_)g?vcf($|_)", "VCF.gz"), (r"_index$", "TBI"),
    (r"log$|_log($|_)|manifest|_list$|summary_text|metric_text", "TXT"),
    (r"(^|_)b?am($|_)|bam_file", "BAM"), (r"cram", "CRAM"),
    (r"archive|tar_gz|_tgz", "TAR.GZ"), (r"newick", "NEWICK"),
    (r"(^|_)maf($|_)", "MAF"), (r"(^|_)bed($|_)", "BED"),
    (r"table|matrix|_tsv|genes$|ranking|prevalence|assignment|edges|mapping|"
     r"fraction|quantification|calls|report|metrics|qc|summary|enrichment|"
     r"^go_|^reactome_|composition|callability|events?", "TSV"),
]
# 这些 artifact_type 是容器内自带的参考资源，缺了不算缺必填
REFERENCE_ARTIFACTS = {"reference_fasta", "reference_fai", "reference_dict", "reference_vcf",
                       "star_index", "rsem_index", "gtf", "interval_list", "dependency_archive"}
# 参数名兜底：交付包里有一批参数 artifact_type 和描述都不带格式，只能按命名惯例推。
NAME_PATTERNS = [
    (r"_r[12]$|_read[12]$|fastq", "FASTQ.gz"), (r"_bams?$|^bam", "BAM"),
    (r"_bai$|_bam_index$", "BAI"), (r"gtf", "GTF"),
    (r"clinical|metainfo|meta_info|sample_info", "CSV"),
]


# 交付包每个工具目录都带 input.json（真实跑通过的路径），扩展名比任何推断都硬。
EXT_FORMAT = [
    (".fastq.gz", "FASTQ.gz"), (".fq.gz", "FASTQ.gz"), (".fastq", "FASTQ"), (".fq", "FASTQ"),
    (".vcf.gz", "VCF.gz"), (".vcf", "VCF"), (".tbi", "TBI"), (".bam", "BAM"), (".bai", "BAI"),
    (".cram", "CRAM"), (".crai", "CRAI"), (".interval_list", "INTERVAL_LIST"),
    (".fasta", "FASTA"), (".fa", "FASTA"), (".fai", "FAI"), (".dict", "DICT"),
    (".gtf", "GTF"), (".bed", "BED"), (".gmt", "GMT"), (".maf", "MAF"), (".ped", "PED"),
    (".h5ad", "H5AD"), (".rds", "RDS"), (".xlsx", "XLSX"), (".xls", "XLS"),
    (".tsv", "TSV"), (".csv", "CSV"), (".txt", "TXT"), (".tar.gz", "TAR.GZ"), (".zip", "ZIP"),
]


def example_paths(card_dir):
    """从 input.json / example_inputs.json 里取「参数名 -> 实际文件路径」。

    键形如 `Workflow.param`，取最后一段。有的参数 artifact_type 和描述都不写格式
    （gene_order「基因排序文件」），只有这里的真实路径能定格式。
    """
    out = {}
    for fn in ("example_inputs.json", "input.json", "input_template.json"):
        p = os.path.join(card_dir, fn)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for k, v in (d or {}).items() if isinstance(d, dict) else []:
            v = v[0] if isinstance(v, list) and v and isinstance(v[0], str) else v
            if isinstance(v, str) and "/" in v:
                out.setdefault(k.rsplit(".", 1)[-1], v)
    return out


def fmt_of_path(path):
    low = (path or "").lower()
    for ext, f in EXT_FORMAT:
        if low.endswith(ext):
            return f
    return None


def wdl_types(wdl_path):
    """扒 WDL 顶层 workflow 的 input {} 块：参数名 -> 声明类型。"""
    if not wdl_path or not os.path.exists(wdl_path):
        return {}
    src = open(wdl_path, encoding="utf-8", errors="replace").read()
    m = re.search(r"^workflow\s+\w+\s*\{", src, re.M)
    if not m:
        return {}
    blk = re.search(r"input\s*\{", src[m.end():])
    if not blk:
        return {}
    start = m.end() + blk.end()
    depth, i = 1, start
    while i < len(src) and depth:
        depth += (src[i] == "{") - (src[i] == "}")
        i += 1
    out = {}
    for line in src[start:i - 1].splitlines():
        line = line.split("#")[0].strip()
        d = re.match(r"([A-Za-z]\w*(?:\[[^\]]+\])?[+?]*)\s+(\w+)\s*(=|$)", line)
        if d:
            out[d.group(2)] = d.group(1)
    return out


def to_wdl_type(yaml_type, required):
    base = {"string": "String", "int": "Int", "float": "Float", "boolean": "Boolean",
            "file": "File", "array[string]": "Array[String]", "array[string]+": "Array[String]+",
            "array[file]": "Array[File]", "array[file]+": "Array[File]+"}.get(
        (yaml_type or "").lower(), "String")
    return base if required or base.endswith("+") else base + "?"


def fmt_of(name, artifact, default=None, desc=None, example=None):
    for probe in (name, artifact or ""):
        low = probe.lower()
        for suf, f in SUFFIX_FORMAT:
            if low.endswith(suf) or low == suf.lstrip("_"):
                return f
    if isinstance(default, str):
        for suf, f in SUFFIX_FORMAT:
            if default.lower().endswith(suf):
                return f
    if artifact in ARTIFACT_FORMAT:
        return ARTIFACT_FORMAT[artifact]
    # 有一批参数 artifact_type 是空的，格式只写在中文描述里（"测序数据 R1 文件 (FASTQ.gz)"）。
    for tok in ("FASTQ.gz", "FASTQ", "BAM", "BAI", "VCF", "GTF", "MAF", "BED",
                "RDS", "TSV", "CSV", "XLSX", "H5AD"):
        if desc and re.search(rf"\b{re.escape(tok)}\b", desc, re.I):
            return tok
    return fmt_of_path(example) or next(
        (f for pat, f in NAME_PATTERNS + ARTIFACT_PATTERNS
         if re.search(pat, (artifact or "").lower()) or re.search(pat, name.lower())), None)


def build_one(card_dir, old):
    y = yaml.safe_load(open(os.path.join(card_dir, "knowledge_card.yaml"), encoding="utf-8"))
    meta, iface = y.get("meta") or {}, y.get("interface") or {}
    exe = y.get("execution") or {}
    wdl = (exe.get("files") or {}).get("wdl")
    wt = wdl_types(os.path.join(card_dir, wdl) if wdl else None)
    ex = example_paths(card_dir)

    params = iface.get("params") or []
    require_any = [sorted(v["params"]) for v in iface.get("validators") or []
                   if v.get("type") == "one_of" and v.get("params")]
    named_by_validator = {n for grp in require_any for n in grp}
    keep_in = {p["name"] for p in params if p.get("expose_level") == "basic"}
    keep_in |= {i["name"] for i in (old or {}).get("inputs", [])}   # 只增不减
    old_fmt = {i["name"]: i.get("format") for i in (old or {}).get("inputs", [])}

    inputs = []
    for p in params:
        n = p["name"]
        if n not in keep_in:
            continue
        # target=None 的是高层参数（output_base 只写进 option.json，sample_name 走 resolver），
        # 不是 workflow 输入。只有被 one_of 点名的才收，否则 require_any 会引用到不存在的参数。
        if p.get("target") is None and n not in named_by_validator and n not in old_fmt:
            continue
        art = p.get("artifact_type")
        entry = {"name": n,
                 "type": wt.get(n) or to_wdl_type(p.get("type"), p.get("required")),
                 "format": old_fmt.get(n) or fmt_of(n, art, p.get("default"),
                                                    p.get("description"), ex.get(n)),
                 "required": bool(p.get("required"))}
        # 判据只有两条硬证据：artifact_type 属于参考资源，或卡片给了容器内绝对路径默认值。
        # 不许按名字猜——bcftools 的 filtered_vcf_index 名字带 index，却是数据文件的伴随
        # 索引（default 为空），判错了就既不映射也不报缺，执行阶段才炸。
        if art in REFERENCE_ARTIFACTS or (isinstance(p.get("default"), str)
                                          and p["default"].startswith("/")):
            entry["reference_resource"] = True
        inputs.append(entry)

    old_ofmt = {o["name"]: o.get("format") for o in (old or {}).get("outputs", [])}
    keep_out = {o["name"] for o in y.get("outputs") or [] if o.get("expose_level") == "primary"}
    keep_out |= set(old_ofmt)
    outputs = [{"name": o["name"],
                "type": {"file": "File", "array[file]": "Array[File]"}.get(
                    (o.get("type") or "file").lower(), "File"),
                "format": old_ofmt.get(o["name"]) or fmt_of(o["name"], o.get("artifact_type"),
                                                            desc=o.get("description"))}
               for o in y.get("outputs") or [] if o["name"] in keep_out]

    card = {"graph_tool_id": None,   # 调用方填
            "name_cn": meta.get("name_cn") or meta.get("name"),
            "category": meta.get("category") or "",
            "reusable_for": (old or {}).get("reusable_for", []),
            "inputs": inputs, "outputs": outputs}
    docker = next((p.get("default") for p in params
                   if p["name"] in ("docker_image", "analysis_image")
                   and isinstance(p.get("default"), str)), None)
    tag = (old or {}).get("docker_image_tag") or (
        re.search(r"(task\d{3}_[\w.-]+?)(?::|$)", docker).group(1)
        if docker and re.search(r"task\d{3}_", docker) else None)
    if tag:
        card["docker_image_tag"] = tag
    if require_any:
        card["require_any"] = require_any
    return meta.get("id") or os.path.basename(card_dir), card


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", help="解压后的交付包根目录")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    graph_tools = set()
    with open(CATALOG_PATH, encoding="utf-8") as f:
        import csv as _csv
        for r in _csv.DictReader(f):
            t = (r.get("tool_id") or "").replace("tool_id:", "").strip()
            if t:
                graph_tools.add(t)

    dirs = {}
    for root, _, files in os.walk(a.bundle):
        if "knowledge_card.yaml" in files and "__MACOSX" not in root:
            dirs[os.path.basename(root)] = root

    old_map = json.load(open(MAP_PATH, encoding="utf-8"))
    old_by_tool = {v["graph_tool_id"]: (k, v) for k, v in old_map.items()}

    new_map, skipped = {}, []
    for t in sorted(graph_tools):
        d = dirs.get(t)
        if not d:
            skipped.append(t)
            continue
        old_key, old = old_by_tool.get(t, (None, None))
        mid, card = build_one(d, old)
        card["graph_tool_id"] = t
        if old_key and old_key != mid:
            sys.exit(f"✗ {t}: 卡片 meta.id 从 {old_key} 变成 {mid}，"
                     f"validate_execution_chain 的 by_step.tool_id 会跟着变，先人工确认")
        new_map[mid] = card

    print(f"图上 {len(graph_tools)} 个工具 -> 生成 {len(new_map)} 张卡"
          f"（原 {len(old_map)} 张）", f"交付包里找不到：{skipped}" if skipped else "")
    for mid, c in new_map.items():
        t = c["graph_tool_id"]
        o = old_by_tool.get(t, (None, None))[1]
        if not o:
            print(f"  + 新增 {mid:<32} 入{len(c['inputs'])} 出{len(c['outputs'])}")
            continue
        ni = {i["name"] for i in c["inputs"]} - {i["name"] for i in o["inputs"]}
        no = {i["name"] for i in c["outputs"]} - {i["name"] for i in o["outputs"]}
        if ni or no or c.get("require_any"):
            bits = []
            if ni:
                bits.append(f"入参+{sorted(ni)}")
            if no:
                bits.append(f"出参+{sorted(no)}")
            if c.get("require_any"):
                bits.append(f"require_any={c['require_any']}")
            print(f"  ~ 更新 {mid:<32} {' '.join(bits)}")

    if a.write:
        with open(MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(new_map, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print(f"\n已写入 {MAP_PATH}")
    else:
        print("\n（未写入，加 --write 生效）")


if __name__ == "__main__":
    main()
