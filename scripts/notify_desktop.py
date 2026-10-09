#!/usr/bin/env python3
"""Send one silent local desktop notification to this installation."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--title', required=True, help='通知标题，最多80字符')
    parser.add_argument('--message', required=True, help='纯文字结果，最多400字符')
    parser.add_argument('--id', help='可选去重标识，最多128字符')
    args = parser.parse_args()
    from ui.local_notifications import send_notice
    try:
        result = send_notice(args.title, args.message, args.id)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('status') == 'queued' else 1


if __name__ == '__main__':
    raise SystemExit(main())
