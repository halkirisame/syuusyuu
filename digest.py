"""alphaXiv の話題論文を取得し、Claude で日本語ダイジェスト (MD/HTML) を作る。"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import anthropic
import markdown

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
N_PAPERS = int(os.environ.get("N_PAPERS", "20"))
CATEGORIES = ["cs.AI", "cs.LG", "cs.CL", "cs.CV"]
ALPHAXIV_FEEDS = [
    "https://api.alphaxiv.org/papers/v3/feed?pageNum=0&sort=Hot&pageSize={n}",
    "https://api.alphaxiv.org/papers/v2/feed?pageNum=0&sortBy=Hot&pageSize={n}",
]
UA = {"User-Agent": "syuusyuu-digest/1.0"}
ATOM = "{http://www.w3.org/2005/Atom}"
ID_RE = re.compile(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b")


def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def alphaxiv_ids(n):
    """alphaXiv のトレンドから arXiv ID を抽出（レスポンス形式に依存しないよう正規表現で拾う）。"""
    for url in ALPHAXIV_FEEDS:
        try:
            text = get(url.format(n=n * 2))
            ids = list(dict.fromkeys(ID_RE.findall(text)))
            if ids:
                print(f"alphaXiv: {len(ids)} ids from {url}")
                return ids
        except Exception as e:
            print(f"alphaXiv failed ({url}): {e}", file=sys.stderr)
    return []


def parse_arxiv(xml_text):
    papers = []
    for e in ET.fromstring(xml_text).findall(f"{ATOM}entry"):
        pid = ID_RE.search(e.findtext(f"{ATOM}id", ""))
        if not pid:
            continue
        papers.append({
            "id": pid.group(1),
            "title": " ".join(e.findtext(f"{ATOM}title", "").split()),
            "authors": [a.findtext(f"{ATOM}name") for a in e.findall(f"{ATOM}author")][:5],
            "abstract": " ".join(e.findtext(f"{ATOM}summary", "").split()),
            "category": getattr(e.find("{http://arxiv.org/schemas/atom}primary_category"), "attrib", {}).get("term", ""),
        })
    return papers


def arxiv_by_ids(ids):
    q = urllib.parse.urlencode({"id_list": ",".join(ids), "max_results": len(ids)})
    return parse_arxiv(get(f"https://export.arxiv.org/api/query?{q}"))


def arxiv_recent(n):
    cat = " OR ".join(f"cat:{c}" for c in CATEGORIES)
    q = urllib.parse.urlencode({"search_query": cat, "sortBy": "submittedDate",
                                "sortOrder": "descending", "max_results": n})
    return parse_arxiv(get(f"https://export.arxiv.org/api/query?{q}"))


def collect():
    ids = alphaxiv_ids(N_PAPERS)
    if ids:
        try:
            papers = arxiv_by_ids(ids[: N_PAPERS * 2])
            if papers:
                return papers, "alphaXiv トレンド"
        except Exception as e:
            print(f"arXiv lookup failed: {e}", file=sys.stderr)
    time.sleep(3)  # arXiv API のマナー
    return arxiv_recent(150), "arXiv 新着（Claude が選定）"


PROMPT = """あなたは研究動向に詳しいリサーチアシスタントです。
以下の論文候補から、話題性・新規性・インパクトが大きそうなものを最大{n}本選び、
印刷して読む日本語ダイジェストを Markdown で作ってください。

形式:
- 冒頭に「## 今日のハイライト」として全体傾向を3行程度
- 各論文は以下（インパクト順）:
### N. 日本語タイトル
**原題**: ... / **arXiv**: ID / **分野**: ...
- **一言で**: 1文
- **何をした**: 2〜3文
- **なぜ凄い**: 1〜2文
- **注目度**: ★1〜5
前置きや締めの挨拶は不要。

候補:
{papers}"""


def summarize(papers):
    client = anthropic.Anthropic()
    body = "\n\n".join(
        f"[{p['id']}] {p['title']} ({p['category']})\n著者: {', '.join(p['authors'])}\n{p['abstract']}"
        for p in papers
    )
    with client.messages.stream(
        model=MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": PROMPT.format(n=N_PAPERS, papers=body)}],
    ) as s:
        msg = s.get_final_message()
    return "".join(b.text for b in msg.content if b.type == "text")


CSS = """
@page { size: A4; margin: 15mm; }
body { font-family: 'Noto Sans CJK JP', 'Noto Sans JP', sans-serif; font-size: 10pt; line-height: 1.55; }
h1 { font-size: 16pt; border-bottom: 2px solid #333; }
h2 { font-size: 13pt; margin-top: 1em; }
h3 { font-size: 11.5pt; margin: 1.1em 0 .3em; break-after: avoid; }
ul { margin: .2em 0; padding-left: 1.2em; }
h3 + p, h3 + p + ul { break-inside: avoid; }
"""


def main():
    today = dt.date.today().isoformat()
    papers, source = collect()
    print(f"{len(papers)} candidates ({source})")
    if not papers:
        sys.exit("no papers fetched")
    os.makedirs("digests", exist_ok=True)
    if not os.environ.get("ANTHROPIC_API_KEY"):  # キー未設定時は候補だけ保存
        with open(f"digests/candidates-{today}.json", "w", encoding="utf-8") as f:
            json.dump({"source": source, "papers": papers}, f, ensure_ascii=False, indent=1)
        print("ANTHROPIC_API_KEY not set; saved candidates only")
        return
    md_text = f"# 論文ダイジェスト {today}\n\n出典: {source} / 要約: Claude\n\n" + summarize(papers)
    for p in papers:  # arXiv ID をリンク化
        md_text = md_text.replace(p["id"], f"[{p['id']}](https://arxiv.org/abs/{p['id']})", 1)
    os.makedirs("digests", exist_ok=True)
    with open(f"digests/{today}.md", "w", encoding="utf-8") as f:
        f.write(md_text)
    html = markdown.markdown(md_text, extensions=["extra"])
    with open(f"digests/{today}.html", "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><meta charset='utf-8'><style>{CSS}</style><body>{html}</body>")
    print(f"digests/{today}.html")


if __name__ == "__main__":
    main()
