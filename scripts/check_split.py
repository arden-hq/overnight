"""Check that generated family cases and the hand-written held-out cases do not overlap.

    .venv/bin/python scripts/check_split.py .local/families/order-resolution

The check lives in `overnight.families.split`.
"""

from overnight.families.split import main

if __name__ == "__main__":
    main()
