#!/usr/bin/env python3
"""Export NINEIGHT (YM2608 / OPNA) patches as Furnace instrument files (.fui).

Written from Furnace's public file-format notes (papers/newIns.md); no Furnace
code is used. Standard library only.

    nin8fui.py bank OUTDIR                  all bundled presets -> OUTDIR/NN_name.fui
    nin8fui.py convert IN... -o OUT         preset JSON / '#patch=' URL / APVTS-id JSON -> .fui
    nin8fui.py inspect FILE.fui... [--json] decode a .fui back into NINEIGHT terms
    nin8fui.py refresh-bank                 (dev) rebuild nin8_presets.json from ui/webui.zip

What travels in the file (all 44 per-voice fields, so the instrument sounds the
same as in nin8 when Furnace's YM2608 uses the ymfm core at 8 MHz):
    algorithm, feedback, AMS, PMS and per operator AR DR SR RR SL TL KS MUL DT AM SSG-EG.
Plus LFO speed as an instrument macro when the patch uses the LFO (AMS/PMS != 0).
What does not (it isn't instrument data in Furnace): pan, master gain, polyphony.

Two encoding details that are easy to get wrong:
  * Furnace keeps operators in register-slot order 1,3,2,4, so nin8 op2/op3 swap.
  * Furnace stores detune as signed-detune + 3, not as the chip's DT register code.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import struct
import sys
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
BANK_PATH = HERE / "nin8_presets.json"
UI_ZIP = HERE.parent / "ui" / "webui.zip"

FUI_MAGIC = b"FINS"
INS_TYPE_FM = 1                    # "FM (OPN)": what YM2608 FM1-6 prefer
DEFAULT_VERSION = 228              # Furnace 0.6.8.x; newer builds read it without complaint
MIN_VERSION, MAX_VERSION = 224, 256  # 224 added the block byte this writer always emits
KVS_AUTO = 2                       # "TL volume scale: auto"; 0 would stop note volume scaling the carriers

OP_FIELDS = ("ar", "dr", "sr", "rr", "sl", "tl", "ks", "mul", "dt", "am", "ssgeg")
OP_MAX = {"ar": 31, "dr": 31, "sr": 31, "rr": 15, "sl": 15, "tl": 127,
          "ks": 3, "mul": 15, "dt": 7, "am": 1, "ssgeg": 15}

# chip DT register code -> Furnace's stored value, and the inverse (Furnace's dtTable).
DT_REG_TO_FUR = (3, 4, 5, 6, 7, 2, 1, 0)
DT_FUR_TO_REG = (7, 6, 5, 0, 1, 2, 3, 4)
# Furnace op[i] holds nin8 op FUR_OP_ORDER[i] (register-slot order 1,3,2,4).
FUR_OP_ORDER = (0, 2, 1, 3)

MACRO_EX3_LFO_SPEED = 7            # macro code of the OPN "LFO speed" macro


class PatchError(ValueError):
    """Bad or out-of-range patch data."""


@dataclass
class Patch:
    name: str
    alg: int
    fb: int
    ams: int
    pms: int
    lfo_freq: int
    ops: list = field(default_factory=list)   # 4 dicts keyed by OP_FIELDS

    def validate(self) -> "Patch":
        _check("algorithm", self.alg, 7)
        _check("feedback", self.fb, 7)
        _check("lfo ams", self.ams, 3)
        _check("lfo pms", self.pms, 7)
        _check("lfo freq", self.lfo_freq, 7)
        if len(self.ops) != 4:
            raise PatchError(f"expected 4 operators, got {len(self.ops)}")
        for n, op in enumerate(self.ops, 1):
            for f in OP_FIELDS:
                if f not in op:
                    raise PatchError(f"operator {n} is missing '{f}'")
                _check(f"op{n} {f}", op[f], OP_MAX[f])
        return self

    @property
    def uses_lfo(self) -> bool:
        return bool(self.ams or self.pms)

    def to_preset_json(self) -> dict:
        """The nin8 UI's preset schema (Ctrl+D export / drag-and-drop import)."""
        return {
            "name": self.name, "algorithm": self.alg, "feedback": self.fb, "pan": 3,
            "lfo": {"freq": self.lfo_freq, "ams": self.ams, "pms": self.pms},
            "envelopes": [{"ar": o["ar"], "dr": o["dr"], "sr": o["sr"], "sl": o["sl"],
                           "rr": o["rr"], "tl": o["tl"], "mul": o["mul"], "dt": o["dt"],
                           "am": o["am"], "ssgEg": o["ssgeg"], "ks": o["ks"]} for o in self.ops],
        }


def _check(label: str, value, hi: int) -> int:
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise PatchError(f"{label}: expected an integer, got {value!r}")
    if not 0 <= value <= hi:
        raise PatchError(f"{label}={value} is out of range 0..{hi}")
    return value


def _op(src: dict, where: str) -> dict:
    out = {}
    for f in OP_FIELDS:
        key = "ssgEg" if f == "ssgeg" and "ssgEg" in src else f
        if key not in src:
            raise PatchError(f"{where} is missing '{key}'")
        out[f] = _check(f"{where} {f}", src[key], OP_MAX[f])
    return out


# ---------------------------------------------------------------------------
# Input formats
# ---------------------------------------------------------------------------

def patch_from_preset(obj: dict, name: str | None = None) -> Patch:
    """nin8 UI preset JSON: {name, algorithm, feedback, lfo{freq,ams,pms}, pan, envelopes[4]}.

    Also accepts 'operators' as an alias for 'envelopes' and the UI's sequence
    files ({name, instrument: {...preset}, notes: [...]})."""
    if isinstance(obj.get("instrument"), dict):
        inner = dict(obj["instrument"])
        inner.setdefault("name", obj.get("name"))
        return patch_from_preset(inner, name)
    ops = obj.get("envelopes", obj.get("operators"))
    if not isinstance(ops, list) or len(ops) != 4:
        raise PatchError("preset needs an 'envelopes' list with 4 operators")
    lfo = obj.get("lfo") or {}
    return Patch(
        name=_clean_name(name or obj.get("name") or "Custom"),
        alg=_check("algorithm", obj.get("algorithm", obj.get("alg")), 7),
        fb=_check("feedback", obj.get("feedback", obj.get("fb")), 7),
        ams=_check("lfo ams", lfo.get("ams", 0), 3),
        pms=_check("lfo pms", lfo.get("pms", 0), 7),
        lfo_freq=_check("lfo freq", lfo.get("freq", 0), 7),
        ops=[_op(o, f"op{n}") for n, o in enumerate(ops, 1)],
    ).validate()


def patch_from_params(params: dict, name: str = "Custom") -> Patch:
    """nin8's APVTS parameter ids: alg, fb, lfo_freq/ams/pms, op1_ar .. op4_ssgeg."""
    ops = []
    for n in range(1, 5):
        ops.append({f: _check(f"op{n} {f}", params[f"op{n}_{f}"], OP_MAX[f]) for f in OP_FIELDS})
    return Patch(name=_clean_name(name), alg=_check("algorithm", params["alg"], 7),
                 fb=_check("feedback", params["fb"], 7),
                 ams=_check("lfo ams", params["lfo_ams"], 3), pms=_check("lfo pms", params["lfo_pms"], 7),
                 lfo_freq=_check("lfo freq", params["lfo_freq"], 7), ops=ops).validate()


def patch_from_hash(text: str, name: str | None = None) -> Patch:
    """The web app's share link: '...#patch=<base64 JSON>[&name=...]' (or just the base64).

    JSON is {a: algorithm, f: feedback, l: [freq, pms, ams],
             ops: 4 x [ar, dr, sr, rr, sl, tl, mul, dt, ks, am, ssgEg]}  (note the orders)."""
    text = text.strip()
    m = re.search(r"patch=([^&\s]+)", text)
    b64 = urllib.parse.unquote(m.group(1) if m else text.lstrip("#"))
    link_name = re.search(r"[&#?]name=([^&\s]*)", text)
    if name is None and link_name:
        name = urllib.parse.unquote(link_name.group(1))
    b64 = b64.replace("-", "+").replace("_", "/")
    try:
        raw = base64.b64decode(b64 + "=" * (-len(b64) % 4), validate=True)
        d = json.loads(raw.decode("utf-8"))
        ops = d["ops"]
        lfo = d.get("l") or [0, 0, 0]
        if len(ops) != 4 or any(len(o) != 11 for o in ops) or len(lfo) != 3:
            raise ValueError("unexpected shape")
        order = ("ar", "dr", "sr", "rr", "sl", "tl", "mul", "dt", "ks", "am", "ssgeg")
        return Patch(
            name=_clean_name(name or "Custom"), alg=_check("algorithm", d["a"], 7),
            fb=_check("feedback", d["f"], 7), lfo_freq=_check("lfo freq", lfo[0], 7),
            pms=_check("lfo pms", lfo[1], 7), ams=_check("lfo ams", lfo[2], 3),
            ops=[{f: _check(f"op{n} {f}", v, OP_MAX[f]) for f, v in zip(order, o)}
                 for n, o in enumerate(ops, 1)],
        ).validate()
    except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError) as e:
        if isinstance(e, PatchError):
            raise
        raise PatchError(f"not a NINEIGHT '#patch=' link ({type(e).__name__}: {e})") from e


def load_patches(arg: str, name: str | None = None) -> list[Patch]:
    """arg: a file (preset JSON, APVTS-id JSON or text holding a '#patch=' link) or a literal link."""
    path = Path(arg)
    try:
        is_file = path.is_file()
    except OSError:                        # a pasted link is usually longer than NAME_MAX
        is_file = False
    text = path.read_text(encoding="utf-8") if is_file else arg
    fallback = name or (path.stem if is_file else None)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return [patch_from_hash(text, name)]
    if isinstance(doc, dict) and isinstance(doc.get("presets"), list):   # nin8_presets.json style
        doc = doc["presets"]
    items = doc if isinstance(doc, list) else [doc]
    out = []
    for i, d in enumerate(items):
        if not isinstance(d, dict):
            raise PatchError(f"{arg}: expected a JSON object, got {type(d).__name__}")
        if "op1_ar" in d:
            out.append(patch_from_params(d, name or fallback or "Custom"))
        elif "ops" in d and "a" in d:                      # the hash payload as plain JSON
            raw = base64.b64encode(json.dumps(d).encode()).decode()
            out.append(patch_from_hash(raw, name or fallback))
        else:
            out.append(patch_from_preset(d, name if len(items) == 1 else None))
    return out


def _clean_name(name) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", " ", str(name)).strip()
    return name or "Custom"


def safe_filename(name: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "patch"
    return stem[:80]


# ---------------------------------------------------------------------------
# .fui writer / reader
# ---------------------------------------------------------------------------

def _feature(code: bytes, payload: bytes) -> bytes:
    return struct.pack("<2sH", code, len(payload)) + payload


def build_fui(patch: Patch, version: int = DEFAULT_VERSION, lfo_macro: str = "auto") -> bytes:
    """One OPN FM instrument as a .fui. lfo_macro: 'auto' (when AMS/PMS != 0), 'always', 'never'."""
    if not MIN_VERSION <= version <= MAX_VERSION:
        raise PatchError(f"format version must be {MIN_VERSION}..{MAX_VERSION}")
    if lfo_macro not in ("auto", "always", "never"):
        raise PatchError("lfo_macro must be auto, always or never")
    patch.validate()

    fm = bytearray()
    fm.append(0xF4)                                  # bits 7-4: all 4 operators on; low nibble: 4 operators
    fm.append((patch.alg << 4) | patch.fb)
    fm.append((patch.ams << 3) | patch.pms)          # (rest of the byte: OPZ-only LFO selectors, left 0)
    fm.append(0x00)                                  # 2-op/4-op + OPLL preset bits: unused on OPN
    fm.append(0x00)                                  # block 0 = automatic (present from version 224)
    for i in FUR_OP_ORDER:
        o = patch.ops[i]
        fm += bytes((
            (DT_REG_TO_FUR[o["dt"]] << 4) | o["mul"],     # KSR bit (OPM/OPZ) left 0
            o["tl"],                                      # SUS bit left 0
            (o["ks"] << 6) | o["ar"],                     # VIB bit left 0
            (o["am"] << 7) | o["dr"],                     # KSL left 0
            (KVS_AUTO << 5) | o["sr"],                    # EGT left 0; SR is Furnace's "D2R"
            (o["sl"] << 4) | o["rr"],
            o["ssgeg"],                                   # DVB left 0
            0,                                            # DAM / DT2 / WS: not OPN
        ))

    out = bytearray(struct.pack("<4sHH", FUI_MAGIC, version, INS_TYPE_FM))
    out += _feature(b"NA", patch.name.encode("utf-8") + b"\x00")
    out += _feature(b"FM", bytes(fm))
    if lfo_macro == "always" or (lfo_macro == "auto" and patch.uses_lfo):
        # One "ex3" (LFO speed) macro with a single value. Furnace's YM2608 turns that into
        # register 0x22 = 0x08 | speed, matching what nin8 writes. Without it the instrument
        # would inherit whatever speed the previous instrument left in the shared LFO.
        macro = (struct.pack("<H", 8)                              # per-macro header size
                 + bytes((MACRO_EX3_LFO_SPEED, 1, 255, 255, 0, 0, 0, 1))  # code len loop rel mode type delay speed
                 + bytes((patch.lfo_freq,))                        # the value
                 + b"\xff")                                        # end of macros
        out += _feature(b"MA", macro)
    return bytes(out)


def parse_fui(data: bytes) -> tuple[Patch, dict]:
    """Decode an OPN FM .fui (ours, or one Furnace saved) into a Patch plus {'version','type'}."""
    if data[:4] != FUI_MAGIC or len(data) < 8:
        raise PatchError("not a Furnace instrument file (expected 'FINS'; the pre-0.6 format is not supported)")
    version, ins_type = struct.unpack_from("<HH", data, 4)
    if ins_type != INS_TYPE_FM:
        raise PatchError(f"instrument type {ins_type} is not OPN FM (1)")
    name, fm, lfo_freq = "", None, 0
    pos = 8
    while pos + 2 <= len(data):
        code = data[pos:pos + 2]
        if code == b"EN":
            break
        if pos + 4 > len(data):
            raise PatchError("truncated feature header")
        (size,) = struct.unpack_from("<H", data, pos + 2)
        body = data[pos + 4:pos + 4 + size]
        if len(body) != size:
            raise PatchError(f"feature {code!r} is truncated")
        if code == b"NA":
            name = body.split(b"\x00", 1)[0].decode("utf-8", "replace")
        elif code == b"FM":
            fm = _parse_fm(body, version)
        elif code == b"MA":
            lfo_freq = _parse_ex3(body)
        pos += 4 + size
    if fm is None:
        raise PatchError("no FM feature in file")
    alg, fb, ams, pms, fur_ops = fm
    ops = [None] * 4
    for slot, o in enumerate(fur_ops):
        ops[FUR_OP_ORDER[slot]] = o
    return (Patch(_clean_name(name or "Custom"), alg, fb, ams, pms, lfo_freq, ops).validate(),
            {"version": version, "type": ins_type, "named": bool(name)})


def _parse_fm(b: bytes, version: int):
    if len(b) < 4:
        raise PatchError("FM feature too short")
    op_count = b[0] & 15
    pos = 4 + (1 if version >= 224 else 0)
    if op_count < 4 or len(b) < pos + 8 * op_count:
        raise PatchError("FM feature is not a 4-operator OPN voice")
    alg, fb = (b[1] >> 4) & 7, b[1] & 7
    ams, pms = (b[2] >> 3) & 3, b[2] & 7
    ops = []
    for i in range(4):
        r = b[pos + 8 * i:pos + 8 * i + 8]
        ops.append({"dt": DT_FUR_TO_REG[(r[0] >> 4) & 7], "mul": r[0] & 15, "tl": r[1] & 127,
                    "ks": (r[2] >> 6) & 3, "ar": r[2] & 31, "am": (r[3] >> 7) & 1, "dr": r[3] & 31,
                    "sr": r[4] & 31, "sl": (r[5] >> 4) & 15, "rr": r[5] & 15, "ssgeg": r[6] & 15})
    return alg, fb, ams, pms, ops


def _parse_ex3(b: bytes) -> int:
    """First value of the ex3 (LFO speed) macro, or 0 when there is none."""
    if len(b) < 2:
        return 0
    (hdr,), pos = struct.unpack_from("<H", b, 0), 2
    while pos < len(b) and b[pos] != 255:
        if pos + hdr > len(b):
            break
        code, length = b[pos], b[pos + 1]
        word = (1, 1, 2, 4)[(b[pos + 5] >> 6) & 3]       # header bytes: code len loop rel mode OPEN/TYPE/WORD delay speed
        body = pos + hdr
        if code == MACRO_EX3_LFO_SPEED and length and body < len(b):
            return b[body] & 7
        pos = body + length * word
    return 0


# ---------------------------------------------------------------------------
# Bundled presets
# ---------------------------------------------------------------------------

def load_bank() -> list[Patch]:
    try:
        doc = json.loads(BANK_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise PatchError(f"{BANK_PATH.name} is missing; run 'nin8fui.py refresh-bank'") from e
    return [patch_from_preset(p) for p in doc["presets"]]


def extract_bundle_presets(js: str) -> list[dict]:
    """Pull every preset literal out of the UI's minified bundle. Dev-time only (refresh-bank/tests)."""
    spreads = set(re.findall(r"lfo:\{\.\.\.([A-Za-z_$][\w$]*)\}", js))
    for var in spreads:
        if not re.search(r"[,;\s]" + re.escape(var) + r"=\{freq:0,ams:0,pms:0\}", js):
            raise PatchError(f"preset LFO spreads '{var}' but its definition is no longer all-zero/obvious")
    presets = []
    for m in re.finditer(r'\{name:"', js):
        end = _match_brace(js, m.start())
        lit = js[m.start():end]
        if "envelopes:[" not in lit or "algorithm:" not in lit:
            continue
        for var in spreads:
            lit = lit.replace("lfo:{..." + var + "}", "lfo:{freq:0,ams:0,pms:0}")
        as_json = re.sub(r"([{,])([A-Za-z_$][\w$]*):", r'\1"\2":', lit)
        try:
            presets.append(json.loads(as_json))
        except json.JSONDecodeError as e:
            raise PatchError(f"preset literal no longer plain JSON-like ({e}): {lit[:80]}...") from e
    return [p for p in presets if p.get("name") != "Init"]


def _match_brace(s: str, start: int) -> int:
    depth, i, in_str = 0, start, None
    while i < len(s):
        c = s[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == in_str:
                in_str = None
        elif c in "\"'`":
            in_str = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise PatchError("unbalanced braces in bundle")


def bundle_presets_from_zip(zip_path: Path = UI_ZIP) -> list[dict]:
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if re.fullmatch(r"assets/index-[^/]+\.js", n)]
        if len(names) != 1:
            raise PatchError(f"expected exactly one assets/index-*.js in {zip_path.name}, found {names}")
        return extract_bundle_presets(z.read(names[0]).decode("utf-8"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _write(path: Path, patch: Patch, args) -> None:
    path.write_bytes(build_fui(patch, args.format_version, args.lfo_macro))
    print(f"{path}  ({patch.name})")


def cmd_bank(args) -> None:
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    bank = load_bank()
    width = len(str(len(bank)))
    wanted = {n.lower() for n in args.only}
    count = 0
    for i, p in enumerate(bank, 1):
        if wanted and p.name.lower() not in wanted:
            continue
        _write(out / f"{i:0{width}d}_{safe_filename(p.name)}.fui", p, args)
        count += 1
    if wanted and not count:
        raise PatchError("no bundled preset matches " + ", ".join(args.only))
    print(f"{count} instrument(s) written. In Furnace: Instruments > Open, multi-select to load them all.")


def cmd_convert(args) -> None:
    patches = []
    for src in args.inputs:
        patches += load_patches(src, args.name if len(args.inputs) == 1 else None)
    target = Path(args.output)
    as_dir = target.is_dir() or str(args.output).endswith(("/", "\\")) or len(patches) > 1
    if as_dir:
        target.mkdir(parents=True, exist_ok=True)
        for p in patches:
            _write(target / f"{safe_filename(p.name)}.fui", p, args)
    else:
        _write(target, patches[0], args)


def cmd_inspect(args) -> None:
    for f in args.files:
        patch, meta = parse_fui(Path(f).read_bytes())
        if not meta["named"]:                   # Furnace omits the name block unless told to store it
            patch.name = _clean_name(Path(f).stem)
        if args.json:
            print(json.dumps(patch.to_preset_json(), indent=2))
            continue
        print(f"{f}: '{patch.name}'  format v{meta['version']}")
        print(f"  algorithm {patch.alg}  feedback {patch.fb}  LFO freq {patch.lfo_freq} ams {patch.ams} pms {patch.pms}")
        print("  op  " + "  ".join(f"{k.upper():>5}" for k in OP_FIELDS))
        for n, o in enumerate(patch.ops, 1):
            print(f"  {n}   " + "  ".join(f"{o[k]:>5}" for k in OP_FIELDS))


def cmd_refresh_bank(args) -> None:
    presets = bundle_presets_from_zip(Path(args.ui))
    for p in presets:
        patch_from_preset(p)                                 # range-check every one
    doc = {"generated_by": "scripts/nin8fui.py refresh-bank",
           "source": "ui/webui.zip assets/index-*.js",
           "note": "Presets carry no origin or licence metadata in the UI bundle.",
           "presets": presets}
    BANK_PATH.write_text("{\n \"generated_by\": %s,\n \"source\": %s,\n \"note\": %s,\n \"presets\": [\n%s\n ]\n}\n" % (
        json.dumps(doc["generated_by"]), json.dumps(doc["source"]), json.dumps(doc["note"]),
        ",\n".join("  " + json.dumps(p, separators=(",", ":")) for p in presets)), encoding="utf-8")
    print(f"{len(presets)} presets -> {BANK_PATH}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="nin8fui", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--format-version", type=int, default=DEFAULT_VERSION,
                       help=f".fui format version to write (default {DEFAULT_VERSION}, Furnace 0.6.8.x)")
        p.add_argument("--lfo-macro", choices=("auto", "always", "never"), default="auto",
                       help="add the LFO-speed macro: auto = when AMS/PMS is non-zero (default)")

    p = sub.add_parser("bank", help="export every bundled preset")
    p.add_argument("outdir")
    p.add_argument("--only", nargs="+", default=[], metavar="NAME", help="only presets with these names")
    common(p)
    p.set_defaults(fn=cmd_bank)

    p = sub.add_parser("convert", help="convert preset JSON / '#patch=' links to .fui")
    p.add_argument("inputs", nargs="+", help="file, or a literal '#patch=...' link / base64")
    p.add_argument("-o", "--output", required=True, help="output .fui file, or a directory")
    p.add_argument("--name", help="instrument name (single input only)")
    common(p)
    p.set_defaults(fn=cmd_convert)

    p = sub.add_parser("inspect", help="decode .fui files")
    p.add_argument("files", nargs="+")
    p.add_argument("--json", action="store_true", help="print as a NINEIGHT preset JSON")
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("refresh-bank", help="(dev) rebuild nin8_presets.json from the UI bundle")
    p.add_argument("--ui", default=str(UI_ZIP))
    p.set_defaults(fn=cmd_refresh_bank)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (PatchError, OSError, zipfile.BadZipFile) as e:
        print(f"nin8fui: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
