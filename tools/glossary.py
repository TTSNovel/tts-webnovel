"""Publishes a book's translation glossary to Google Cloud Translation (v3).

    python tools/glossary.py push 119 tools/data/glossaries/119.en-vi.tsv
    python tools/glossary.py show 119

The TSV (two tab-separated columns: source term, target term; `#` lines
are comments) is uploaded next to the book's stored translations on the
site bucket (translations/glossaries/<book>.<src>-<tgt>.tsv — see
app/translation.py) and a Google glossary `novel-book-<book>-<src>-<tgt>` is
re-created from it. Its submitTime is the revision app/translation.py
stamps on every stored segment — so pushing a changed glossary makes
previously translated segments re-translate the next time they're read,
without having to delete anything.

Uses Application Default Credentials (`gcloud auth application-default
login`), same as the server.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import translation  # noqa: E402


def _clean_tsv(text):
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) != 2 or not cols[0].strip() or not cols[1].strip():
            raise SystemExit(f"bad glossary line (need 'source<TAB>target'): {line!r}")
        rows.append(f"{cols[0].strip()}\t{cols[1].strip()}")
    return "\n".join(rows) + "\n"


def _wait(op_name, headers):
    while True:
        op = requests.get(f"{translation._API}/{op_name}", headers=headers, timeout=30).json()
        if op.get("done"):
            if "error" in op:
                raise SystemExit(f"{op_name} failed: {op['error']}")
            return op
        time.sleep(2)


def push(args):
    content = _clean_tsv(Path(args.tsv).read_text(encoding="utf-8"))
    headers, project = translation._headers()
    bucket = args.bucket or f"{project}-novel-web-site"
    object_path = f"translations/glossaries/{args.book_id}.{args.source}-{args.target}.tsv"
    uri = f"gs://{bucket}/{object_path}"

    tmp = Path(args.tsv).with_suffix(".upload.tsv")
    tmp.write_text(content, encoding="utf-8")
    try:
        subprocess.run(["gcloud", "storage", "cp", str(tmp), uri, "--quiet"], check=True)
    finally:
        tmp.unlink()

    gid = translation.glossary_id(args.book_id, args.source, args.target)
    parent = translation._parent(project)
    existing = requests.get(f"{translation._API}/{parent}/glossaries/{gid}", headers=headers, timeout=30)
    if existing.ok:
        resp = requests.delete(f"{translation._API}/{parent}/glossaries/{gid}", headers=headers, timeout=30)
        resp.raise_for_status()
        _wait(resp.json()["name"], headers)

    body = {
        "name": f"{parent}/glossaries/{gid}",
        "languagePair": {"sourceLanguageCode": args.source, "targetLanguageCode": args.target},
        "inputConfig": {"gcsSource": {"inputUri": uri}},
    }
    resp = requests.post(f"{translation._API}/{parent}/glossaries", json=body, headers=headers, timeout=30)
    if not resp.ok:
        raise SystemExit(f"create glossary failed: {resp.status_code} {resp.text}")
    _wait(resp.json()["name"], headers)
    show(args)


def show(args):
    headers, project = translation._headers()
    gid = translation.glossary_id(args.book_id, args.source, args.target)
    resp = requests.get(
        f"{translation._API}/{translation._parent(project)}/glossaries/{gid}", headers=headers, timeout=30
    )
    if resp.status_code == 404:
        print(f"{gid}: no glossary")
        return
    resp.raise_for_status()
    g = resp.json()
    print(f"{gid}: {g.get('entryCount')} entries, revision (submitTime) {g.get('submitTime')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (("push", push), ("show", show)):
        p = sub.add_parser(name)
        p.add_argument("book_id", type=int)
        if name == "push":
            p.add_argument("tsv")
            p.add_argument("--bucket", help="defaults to <project>-novel-web-site")
        p.add_argument("--source", default="en")
        p.add_argument("--target", default="vi")
        p.set_defaults(fn=fn)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
