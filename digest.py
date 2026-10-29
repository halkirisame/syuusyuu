"""alphaXiv の話題論文を取得し、Claude が本文を読んで新聞形式の日本語解説 (HTML) を作る。"""
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
from html.parser import HTMLParser

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
N_PAPERS = int(os.environ.get("N_PAPERS", "8"))
FULLTEXT_CHARS = 60000
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


def fulltext(pid):
    """arXiv の HTML 版から本文テキストを取る。無ければ None。"""
    class Text(HTMLParser):
        skip = 0
        parts = []

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "nav", "header", "footer"):
                self.skip += 1
            if tag == "math":  # 数式は alttext (LaTeX) で残す
                alt = dict(attrs).get("alttext")
                if alt:
                    self.parts.append(f" ${alt}$ ")
                self.skip += 1

        def handle_endtag(self, tag):
            if tag in ("script", "style", "nav", "header", "footer", "math"):
                self.skip = max(0, self.skip - 1)

        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)

    try:
        time.sleep(3)
        p = Text()
        p.parts = []
        p.feed(get(f"https://arxiv.org/html/{pid}", timeout=60))
        text = re.sub(r"\s+", " ", "".join(p.parts))
        return text[:FULLTEXT_CHARS] if len(text) > 2000 else None
    except Exception as e:
        print(f"fulltext failed ({pid}): {e}", file=sys.stderr)
        return None


def ask(client, prompt, max_tokens):
    with client.messages.stream(
        model=MODEL, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as s:
        msg = s.get_final_message()
    return "".join(b.text for b in msg.content if b.type == "text").strip()


SELECT_PROMPT = """以下の論文候補から、話題性・新規性・インパクトが大きく、AIに関心のある一般読者が読んで面白いものを{n}本選んでください。
JSON のみで答えてください（前後に文章や ``` を付けない）:
{{"highlight": "今日の全体傾向を日本語3〜4文で", "picks": ["arXiv ID", ...]}}
picks はインパクト順。

候補:
{papers}"""

ARTICLE_PROMPT = """あなたは科学新聞の記者です。下の論文を読み、AIに関心はあるが専門家ではない読者向けに、
印刷して読む新聞の解説記事を日本語で書いてください。

方針:
- 小さくまとめず、わかりやすさと詳しさを最優先。長さは 5000〜9000 字程度（A4 で 1〜2 枚分）。
- 専門用語は初出で噛み砕いて説明し、重要なものは用語メモにする。たとえ話や具体例を積極的に使う。
- 仕組みはステップごとに順を追って説明する。数式は使わず、言葉と例で説明する。
- 実験結果は具体的な数字を出し、それが何を意味するかを書く。
- 論文に書かれていないことを事実として書かない。編集側の見解は「解説」節に限る。
{source_note}

構成（この順で）:
1. 見出し・サブ見出し・リード文（3〜4文で要点）
2. 背景：何が問題で、なぜ今これが重要か
3. 仕組み：何をどうやったか（ステップごと）
4. 結果：何がわかったか（数字とその意味）
5. 限界・注意点：論文が認めている弱点、読者が気をつけるべき点
6. 解説：なぜ重要か、今後どうなりそうか
7. 一言まとめ

出力は次の HTML 断片のみ（<html>/<body>/<style>/<script> や ``` は書かない）。使ってよいタグとクラス:
<article>
<span class="kicker">分野ラベル</span>
<h2>見出し</h2>
<p class="deck">サブ見出し</p>
<div class="meta">原題：{title}（arXiv {id}）</div>
<p class="leadtext">リード文</p>
<div class="cols">  ← 本文は2段組。中で <h4>節見出し</h4> と <p>段落</p>、<ul><li>、<b>強調</b> を使う
<div class="box"><b>用語メモ：○○</b><p>説明</p></div>   ← 本文中に適宜
<div class="point"><b>📌 ここがポイント</b><p>…</p></div>  ← 1〜2個
</div>
<div class="summary"><b>一言まとめ</b>…<span class="stars">注目度 ★★★★☆</span></div>
</article>

論文情報:
タイトル: {title}
著者: {authors}
分野: {category}
概要: {abstract}

{body}"""

CSS = """
@page { size: A4; margin: 12mm 13mm; }
* { box-sizing: border-box; }
body { margin: 0; color: #111; font-family: 'Noto Serif CJK JP', 'Noto Sans CJK JP', serif; font-size: 10pt; line-height: 1.75; }
.mast { text-align: center; border-bottom: 3px double #111; padding-bottom: 4px; }
.mast h1 { font-size: 32pt; letter-spacing: .35em; margin: 0; }
.mast .sub { display: flex; justify-content: space-between; font-size: 8.5pt; border-top: 1px solid #111; margin-top: 3px; padding-top: 2px; }
.front h3 { font-size: 13pt; border-bottom: 1px solid #111; margin: 10px 0 4px; }
.front li { margin: 3px 0; }
article { break-before: page; }
.kicker { display: inline-block; background: #111; color: #fff; font-size: 8pt; padding: 0 6px; }
h2 { font-size: 20pt; line-height: 1.3; margin: 4px 0; break-after: avoid; }
.deck { font-weight: bold; font-size: 11pt; color: #333; margin: 0 0 4px; }
.meta { font-size: 7.5pt; color: #555; border-bottom: 2px solid #111; padding-bottom: 4px; margin-bottom: 6px; }
.leadtext { font-size: 11pt; font-weight: bold; text-indent: 0; margin: 4px 0 8px; }
.cols { column-count: 2; column-gap: 8mm; column-rule: 1px solid #bbb; }
p { margin: 0 0 5px; text-align: justify; text-indent: 1em; }
h4 { font-size: 11pt; margin: 8px 0 3px; border-left: 4px solid #111; padding-left: 5px; break-after: avoid; }
ul { margin: 2px 0 6px; padding-left: 1.3em; }
.box, .point { border: 1px solid #111; background: #f2f2f2; padding: 5px 7px; margin: 6px 0; font-size: 9pt; break-inside: avoid; }
.point { background: #fff; border-width: 2px; }
.box b, .point b { display: block; }
.box p, .point p { text-indent: 0; margin: 0; }
.summary { border-top: 2px solid #111; margin-top: 8px; padding-top: 4px; font-size: 10.5pt; }
.summary b { margin-right: 1em; }
.stars { float: right; }
.foot { font-size: 7.5pt; color: #555; margin-top: 10px; border-top: 1px solid #bbb; }
"""


def clean(fragment):
    fragment = re.sub(r"^```\w*|```$", "", fragment.strip()).strip()
    return re.sub(r"<(script|style)[\s\S]*?</\1>", "", fragment, flags=re.I)


def build(papers, source):
    client = anthropic.Anthropic()
    listing = "\n\n".join(
        f"[{p['id']}] {p['title']} ({p['category']})\n{p['abstract']}" for p in papers)
    raw = ask(client, SELECT_PROMPT.format(n=N_PAPERS, papers=listing), 2000)
    sel = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    by_id = {p["id"]: p for p in papers}
    picks = [by_id[i] for i in sel["picks"] if i in by_id][:N_PAPERS]
    articles = []
    for p in picks:
        body = fulltext(p["id"])
        note = "" if body else "- 今回は本文が取得できず概要のみ。概要から言える範囲で書き、推測は推測と明記する。"
        print(f"writing {p['id']} (fulltext={'yes' if body else 'no'})")
        articles.append(clean(ask(client, ARTICLE_PROMPT.format(
            source_note=note, title=p["title"], id=p["id"], authors=", ".join(p["authors"]),
            category=p["category"], abstract=p["abstract"],
            body=f"本文:\n{body}" if body else ""), 16000)))
    return sel.get("highlight", ""), picks, articles


def main():
    today = dt.date.today()
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
    highlight, picks, articles = build(papers, source)
    wd = "月火水木金土日"[today.weekday()]
    toc = "".join(f"<li><b>{i}.</b> {p['title']}</li>" for i, p in enumerate(picks, 1))
    html = f"""<!doctype html><html lang="ja"><meta charset="utf-8"><title>AI論文新聞 {today}</title>
<style>{CSS}</style><body>
<div class="mast"><h1>AI論文新聞</h1><div class="sub"><span>{today.year}年{today.month}月{today.day}日（{wd}）</span>
<span>出典：{source}</span><span>編集：Claude</span></div></div>
<div class="front"><h3>今日のハイライト</h3><p>{highlight}</p><h3>本日の記事</h3><ol style="list-style:none;padding:0">{toc}</ol>
<div class="foot">各記事は arXiv 論文をもとに Claude が解説したものです。「解説」節は編集側の見解を含みます。原文は arxiv.org/abs/（ID）で参照できます。</div></div>
{''.join(articles)}
</body></html>"""
    with open(f"digests/{today}.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"digests/{today}.html")


if __name__ == "__main__":
    main()
