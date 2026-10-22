"""구호기관 문서 본문 추출 (페이지 단위) + 개인정보 가림.

    uv run python -m pipeline.doc_text extract {ifrc_go|shelter_cluster} [--limit N]
    uv run python -m pipeline.doc_text report

- 입력: collectors.doc_fetch가 남긴 files_index.jsonl의 성공 기록.
  IFRC GO는 PDF 본체, Shelter Cluster는 문서 페이지에 걸린 첨부 파일(PDF, DOCX)을 읽는다.
  DOC·XLS·PPT(구형 이진 형식)·XLSX·PPTX는 아직 읽지 않고 unsupported로 기록한다.
- 출력: {DERIVED_DIR}/doc_text/<source>.jsonl. 파일 하나당 레코드 하나, pages=[{page, text}].
  text는 pipeline/pii.py로 가린 텍스트다. 근거 offset은 (page, 가린 텍스트 기준 문자 offset)으로 남긴다.
  원문 텍스트는 저장하지 않는다(원본 파일은 RAW_DIR에 내부 보관).
- 휴대전화로 판정된 번호는 data/pii_flags/<source>.jsonl에 문서 URL·쪽·유형·가린 번호만 기록한다.
- 텍스트 층이 없는 PDF(스캔본)는 needs_ocr=True로 표시한다.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import subprocess
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pycountry
import pypdfium2 as pdfium
import zstandard

from pipeline.pii import redact

OCR_MIN_CHARS_PER_PAGE = 30


def _raw(source: str) -> Path:
    return Path(os.environ.get("RAW_DIR", "data/raw")) / source


def _out(name: str) -> Path:
    d = Path(os.environ.get("DERIVED_DIR", "data/derived")) / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().split("\n") if l.strip()] if path.exists() else []


def _code_state() -> dict:
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True).stdout.strip())
    return {"code_commit": head, "code_dirty": dirty}


def pdf_pages(data: bytes) -> list[str]:
    pdf = pdfium.PdfDocument(data)
    try:
        out = []
        for i in range(len(pdf)):
            tp = pdf[i].get_textpage()
            out.append(tp.get_text_bounded().replace("\r\n", "\n").replace("\r", "\n"))
            tp.close()
        return out
    finally:
        pdf.close()


def docx_pages(data: bytes) -> list[str]:
    """DOCX는 쪽 개념이 없어 전체를 1쪽으로 둔다. 문단(w:p) 경계만 줄바꿈으로 살린다."""
    xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8", "replace")
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
    text = "\n".join("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p)) for p in paras)
    return [html.unescape(text)]


def _region_from_group(group: str) -> str | None:
    """Shelter Cluster 대응 그룹 슬러그(예: nepal-earthquake-2015)에서 국가를 찾는다. 못 찾으면 None."""
    words = group.split("-")
    for n in (3, 2, 1):
        try:
            return pycountry.countries.lookup(" ".join(words[:n])).alpha_2
        except LookupError:
            continue
    return None


def _units(source: str, rec: dict) -> list[dict]:
    """한 문서 기록에서 읽을 파일 목록."""
    if source == "ifrc_go":
        return [{"url": rec["url"], "path": rec["path"]}]
    return [{"url": f["url"], "path": f["path"]} for f in rec.get("files", []) if f.get("ok")]


def cmd_extract(a) -> None:
    src = a.source
    sel = {x["doc_id"]: x for x in _jsonl(_raw(src) / "selected.jsonl")}
    out = _out("doc_text") / f"{src}.jsonl"
    flags = Path(os.environ.get("PII_FLAGS_DIR", "data/pii_flags"))
    flags.mkdir(parents=True, exist_ok=True)
    done = {x["file_url"] for x in _jsonl(out)}
    dctx, code = zstandard.ZstdDecompressor(), _code_state()
    n = 0
    c: Counter = Counter()
    with out.open("a") as fh, (flags / f"{src}.jsonl").open("a") as ff:
        for rec in _jsonl(_raw(src) / "files_index.jsonl"):
            if not rec.get("ok"):
                continue
            meta = sel.get(rec["doc_id"], {}).get("meta", {})
            region = meta.get("iso") if src == "ifrc_go" else _region_from_group(meta.get("group", ""))
            for u in _units(src, rec):
                if u["url"] in done:
                    continue
                done.add(u["url"])
                ext = u["path"].split(".")[-2].lower()
                base = {"source": src, "doc_id": rec["doc_id"], "doc_url": rec["url"], "file_url": u["url"],
                        "format": ext, "region": region, "audit": rec.get("audit", False)}
                data = dctx.decompress(Path(u["path"]).read_bytes())
                try:
                    if data[:5] == b"%PDF-":
                        raw_pages = pdf_pages(data)
                    elif ext == "docx":
                        raw_pages = docx_pages(data)
                    else:
                        c["unsupported:" + ext] += 1
                        fh.write(json.dumps({**base, "status": "unsupported", **code}) + "\n")
                        continue
                except Exception as e:  # 깨진 파일·암호 PDF 등
                    c["error"] += 1
                    fh.write(json.dumps({**base, "status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}", **code}) + "\n")
                    continue
                pages, pii = [], Counter()
                for i, t in enumerate(raw_pages, 1):
                    red, finds = redact(t, region)
                    pages.append({"page": i, "text": red})
                    for f in finds:
                        pii[f["kind"]] += 1
                        if f.get("possible_personal"):
                            ff.write(json.dumps({"source": src, "doc_url": rec["url"], "file_url": u["url"], "page": i,
                                                 "number_type": f["number_type"], "masked": f["masked"],
                                                 "flagged_at": datetime.now(timezone.utc).isoformat()},
                                                ensure_ascii=False) + "\n")
                chars = sum(len(p["text"]) for p in pages)
                needs_ocr = ext == "pdf" and bool(pages) and chars / len(pages) < OCR_MIN_CHARS_PER_PAGE
                c["needs_ocr" if needs_ocr else "ok"] += 1
                fh.write(json.dumps({**base, "status": "ok", "n_pages": len(pages), "chars": chars,
                                     "needs_ocr": needs_ocr, "pii_counts": dict(pii), "pages": pages,
                                     "extractor": "pypdfium2 text layer" if ext == "pdf" else "docx xml",
                                     "processed_at": datetime.now(timezone.utc).isoformat(), **code},
                                    ensure_ascii=False) + "\n")
                n += 1
                if a.limit and n >= a.limit:
                    print(dict(c))
                    return
                if n % 500 == 0:
                    print(f"{src}: {n} files {dict(c)}", flush=True)
    print(f"{src}: {n} files {dict(c)}")


def cmd_report(a) -> None:
    for p in sorted(_out("doc_text").glob("*.jsonl")):
        recs = _jsonl(p)
        st = Counter(r["status"] for r in recs)
        ok = [r for r in recs if r["status"] == "ok"]
        print(json.dumps({
            "source": p.stem, "files": len(recs), "status": st.most_common(),
            "needs_ocr": sum(r["needs_ocr"] for r in ok),
            "pages": sum(r["n_pages"] for r in ok),
            "median_chars": sorted(r["chars"] for r in ok)[len(ok) // 2] if ok else None,
            "pii": dict(sum((Counter(r["pii_counts"]) for r in ok), Counter())),
            "unsupported_formats": Counter(r["format"] for r in recs if r["status"] == "unsupported").most_common(),
        }, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser(prog="doc_text")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("extract")
    p.add_argument("source", choices=["ifrc_go", "shelter_cluster"])
    p.add_argument("--limit", type=int)
    p.set_defaults(fn=cmd_extract)
    p = sub.add_parser("report")
    p.set_defaults(fn=cmd_report)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
