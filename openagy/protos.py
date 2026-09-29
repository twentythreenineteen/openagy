"""Regenerate the protocol descriptor reference from the local install.

Nothing at runtime depends on these files — they are reference material
for exploring/validating the wire schema. Rather than shipping extracted
descriptors in the repo, pull them on demand from the user's own Antigravity
installation (the hub UI bundle served by the running language server embeds
every FileDescriptorProto as base64).

Usage:
    python -m openagy.protos [--out DIR]      # default: ./protos

Writes all_XXX.bin files (one FileDescriptorProto each) plus names.json
mapping each index to its proto file name. Read them with a descriptor pool:

    from google.protobuf import descriptor_pool, descriptor_pb2
    pool = descriptor_pool.DescriptorPool()
    for fn in sorted(Path("protos").glob("all_*.bin")):
        fdp = descriptor_pb2.FileDescriptorProto()
        fdp.ParseFromString(fn.read_bytes())
        try:
            pool.Add(fdp)
        except Exception:
            pass  # duplicates across blobs are fine

Requires the Antigravity app to be running (discovery as usual).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path


def extract_descriptors(main_js: str) -> list[tuple[str, bytes]]:
    """Pull every `=Cc("base64...")` FileDescriptorProto from the bundle."""
    out: list[tuple[str, bytes]] = []
    for m in re.finditer(r"=Cc\(", main_js):
        i = m.end()
        parts: list[str] = []
        while True:
            q = main_js.find('"', i)
            if q < 0:
                break
            e = main_js.find('"', q + 1)
            parts.append(main_js[q + 1:e])
            i = e + 1
            if main_js[i:i + 1] == "+":
                i += 1
                while main_js[i].isspace():
                    i += 1
                continue
            break
        b = "".join(parts)
        b += "=" * (-len(b) % 4)
        try:
            raw = base64.b64decode(b)
        except Exception:
            continue
        if not raw.startswith(b"\x0a"):
            continue
        name = raw[2:2 + raw[1]].decode("utf-8", "replace")
        out.append((name, raw))
    return out


def pull(out_dir: Path) -> dict:
    import ssl
    import urllib.request

    from .discover import discover

    target = discover()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(f"{target.base_url}/main.js", headers={
        "x-codeium-csrf-token": target.csrf_token, "User-Agent": "openagy",
    })
    with urllib.request.urlopen(req, context=ctx, timeout=120) as r:
        main_js = r.read().decode("utf-8", "replace")

    descriptors = extract_descriptors(main_js)
    out_dir.mkdir(parents=True, exist_ok=True)
    names: dict[str, str] = {}
    for idx, (name, raw) in enumerate(descriptors):
        fn = f"all_{idx:03d}.bin"
        (out_dir / fn).write_bytes(raw)
        names[fn] = name
    (out_dir / "names.json").write_text(json.dumps(names, indent=1), "utf-8")
    return {"count": len(descriptors), "outDir": str(out_dir),
            "names": names}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="openagy.protos",
        description="pull the wire schema descriptors from the local install")
    ap.add_argument("--out", type=Path, default=Path("protos"),
                    help="output directory (default: ./protos)")
    args = ap.parse_args(argv)
    result = pull(args.out)
    print(f"pulled {result['count']} FileDescriptorProtos into {result['outDir']}")
    print("notable files:")
    seen = set()
    for fn, name in result["names"].items():
        if any(k in name for k in ("language_server", "cortex", "jetbox", "project_pb")):
            if name not in seen:
                seen.add(name)
                print(f"  {fn}: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
