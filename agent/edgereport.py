#!/usr/bin/env python3
"""Retired edge report launcher; never opens logs, credentials or a network connection."""
import sys
import time


def run_once(panel: str, node: str, creds: str, base_log: str,
             verbose: bool = True) -> int:
    """Retired: no log reads, requests, cursor changes, or historical deletion."""
    return 0

def main() -> int:
    """Compatibility launcher: old service arguments are accepted but never read logs/tokens."""
    print('Traffic reporting retired. Disable mediadeck-edgereport.service; historical files are untouched.', flush=True)
    # Old units may use Restart=always. Stay idle instead of creating a restart storm.
    if '--once' in sys.argv:
        return 0
    while True:
        time.sleep(3600)

if __name__ == '__main__':
    raise SystemExit(main())
