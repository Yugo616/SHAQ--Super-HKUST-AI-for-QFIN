"""Reviewed local account-policy activation; preview is the default."""
import argparse
from datetime import datetime
import json
from pathlib import Path

from .virtual_accounts import AccountStore, ET, reconcile_activation


def main(argv=None):
    parser = argparse.ArgumentParser(description='Preview or activate simulation-account continuity')
    parser.add_argument('--account-root', type=Path, required=True)
    parser.add_argument('--rows-json', type=Path, required=True)
    parser.add_argument('--activated-at', default=datetime.now(ET).isoformat())
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    rows = json.loads(args.rows_json.read_text(encoding='utf-8'))
    value = reconcile_activation(AccountStore(args.account_root), rows,
                                 activated_at=args.activated_at, apply=args.apply)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
