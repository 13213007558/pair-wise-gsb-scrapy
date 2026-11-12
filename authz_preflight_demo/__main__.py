from .run_demo import main

if __name__ == "__main__":
    import shutil
    import sys
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="authz-preflight-"))
    print(f"temporary JOBDIR root: {root}")
    try:
        code = main(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    sys.exit(code)
