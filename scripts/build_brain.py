"""Rebuilds the Brain: experiments database and generated wiki pages."""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import brain_db, wiki_generated, wiki_service  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pages-only", action="store_true", help="don't rebuild the database")
    args = parser.parse_args(argv)

    if not args.pages_only:
        started = time.monotonic()

        def progress(done, total):
            if done == total or done % 100 == 0:
                print(f"  analysed {done}/{total} experiments", flush=True)

        counts = brain_db.build(progress)
        print(
            f"experiments database: {brain_db.brain_db_path()} in {time.monotonic() - started:.0f} s"
        )
        for table, rows in counts.items():
            print(f"  {table}: {rows}")

    conn = sqlite3.connect(brain_db.brain_db_path())
    try:
        pages = {
            "chamber-configuration": wiki_generated.chamber_configuration(),
            "experiment-history": wiki_generated.experiment_history(conn),
            "pid-tuning-lessons": wiki_generated.pid_tuning_lessons(conn),
        }
    finally:
        conn.close()
    for page_id, text in pages.items():
        wiki_service.write_generated(page_id, text)
    wiki_service.rebuild_index()
    wiki_service.append_log(
        f"regenerated {', '.join(f'[[{p}]]' for p in pages)} from the data", "build_brain.py"
    )
    print(f"wiki: {wiki_service.wiki_dir()} ({len(wiki_service.list_pages())} pages)")

    problems = wiki_service.lint()
    for problem in problems:
        print(f"  lint: {problem}")
    print("lint: clean" if not problems else f"lint: {len(problems)} problem(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
