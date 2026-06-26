#!/usr/bin/env python3
"""
Verify the settings.json deep-merge used by the installers is safe:
  - preserves the user's existing settings and hooks
  - adds the Claude Mate hooks
  - is idempotent (running it twice changes nothing)

This mirrors the merge logic embedded in packaging/scripts/postinstall and
install/install.sh, run against throwaway temp files (your real settings are
never touched).
"""
import json, os, sys, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNIPPET = os.path.join(REPO, "hooks", "settings.snippet.json")


def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def deep_merge(dst, src):
    for key, val in src.items():
        if key not in dst:
            dst[key] = val
        elif isinstance(val, dict) and isinstance(dst[key], dict):
            deep_merge(dst[key], val)
        elif isinstance(val, list) and isinstance(dst[key], list):
            for item in val:
                if item not in dst[key]:
                    dst[key].append(item)
        else:
            dst[key] = val
    return dst


def merge_into(settings_path):
    settings = load(settings_path)
    snippet = load(SNIPPET)
    settings.setdefault("hooks", {})
    deep_merge(settings["hooks"], dict(snippet.get("hooks", {})))
    with open(settings_path, "w") as fh:
        json.dump(settings, fh, indent=2)
    return settings


def main():
    snippet = load(SNIPPET)
    our_keys = set(snippet.get("hooks", {}))
    assert our_keys, "snippet has no hooks?!"

    checks = []
    def check(name, ok):
        checks.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    tmp = tempfile.mkdtemp(prefix="cm-merge-")

    # Case A: no existing settings file.
    a = os.path.join(tmp, "a.json")
    res = merge_into(a)
    check("A: creates hooks with all Claude Mate keys",
          our_keys.issubset(set(res["hooks"])))

    # Case B: existing unrelated settings + a user's own hook are preserved.
    b = os.path.join(tmp, "b.json")
    with open(b, "w") as fh:
        json.dump({
            "model": "opus",
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {"myOwnHook": {"type": "shell", "events": ["Stop"],
                                     "command": "echo hi"}},
        }, fh)
    res = merge_into(b)
    check("B: preserves unrelated key (model)", res.get("model") == "opus")
    check("B: preserves nested permissions",
          res.get("permissions", {}).get("allow") == ["Bash(ls:*)"])
    check("B: preserves user's own hook", "myOwnHook" in res["hooks"])
    check("B: adds Claude Mate hooks", our_keys.issubset(set(res["hooks"])))

    # Case C: idempotency — merging again yields byte-identical content.
    first = open(b).read()
    merge_into(b)
    second = open(b).read()
    check("C: second merge is a no-op (idempotent)", first == second)

    ok = all(checks)
    print("\n", "ALL PASSED ✅" if ok else "SOME FAILED ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
