from __future__ import annotations

import json
import os
from pathlib import Path

from ukwiki_patrol.ingest.recentchanges import Api

CATEGORIES = ["Категорія:Офіційні правила Вікіпедії", "Категорія:Настанови Вікіпедії"]
OUT = Path("corpus/policies.jsonl")


def main() -> None:
    api = Api(os.environ["WIKI_CONTACT"])
    titles: set[str] = set()
    for cat in CATEGORIES:
        r = api.get(action="query", list="categorymembers", cmtitle=cat, cmnamespace=4, cmlimit=500)
        titles |= {m["title"] for m in r["query"]["categorymembers"]}

    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for title in sorted(titles):
            # one page per request: TextExtracts returns full text only for a single page
            page = api.get(action="query", prop="extracts|info", explaintext=1, titles=title)["query"]["pages"][0]
            doc = {
                "doc_id": title.removeprefix("Вікіпедія:"),
                "title": title,
                "revid": page["lastrevid"],
                "url": f"https://uk.wikipedia.org/wiki/{title.replace(' ', '_')}",
                "text": page["extract"],
            }
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    print(f"{len(titles)} documents -> {OUT}")


if __name__ == "__main__":
    main()
