#!/usr/bin/env python3
"""Tests for nin8fui.py:  python3 scripts/test_nin8fui.py"""
import base64
import contextlib
import io
import json
import random
import struct
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nin8fui as n  # noqa: E402

# nin8's C++ default patch (src/engine/FMPatch.h kDefaultEnvelopes, alg 4 / fb 5):
#                ar dr sr rr sl tl ks mul dt am ssgeg
PIANO_OPS = [(31, 8, 0, 7, 12, 20, 0, 2, 7, 0, 0), (31, 5, 2, 7, 6, 4, 0, 2, 3, 0, 0),
             (31, 7, 4, 7, 10, 18, 0, 2, 3, 0, 0), (31, 0, 4, 7, 0, 0, 0, 2, 0, 0, 0)]
# Golden file: the piano above as Furnace reads it (operators in 1,3,2,4 order, detune remapped, kvs=2).
# Loaded through Furnace's own reader code by an independent verifier.
PIANO_FUI = bytes.fromhex(
    "46494e53 e400 0100 4e41 0b00 6e696e3820 7069616e6f 00 464d 2500 f4 45 00 00 00"
    "02141f08 40c70000 62121f07 44a70000 62041f05 42670000 32001f00 44070000".replace(" ", ""))


def piano(name="nin8 piano", **over) -> n.Patch:
    ops = [dict(zip(n.OP_FIELDS, o)) for o in PIANO_OPS]
    p = n.Patch(name, 4, 5, 0, 0, 0, ops)
    for k, v in over.items():
        setattr(p, k, v)
    return p


def varied(name="varied") -> n.Patch:
    """Every operator field differs between operators and none is 0 or max, so any swapped
    field or operator shows up (the piano has ks/am/ssgeg all 0, which hides such bugs)."""
    ops = []
    for i in range(4):
        ops.append({"ar": 20 + i, "dr": 10 + i, "sr": 5 + i, "rr": 3 + i, "sl": 6 + i, "tl": 30 + 7 * i,
                    "ks": 1 + i % 3, "mul": 2 + 3 * i, "dt": 1 + 2 * i, "am": i % 2, "ssgeg": 8 + i})
    return n.Patch(name, 5, 3, 2, 5, 6, ops)


def random_patch(rng: random.Random, name="r") -> n.Patch:
    return n.Patch(name, rng.randint(0, 7), rng.randint(0, 7), rng.randint(0, 3), rng.randint(0, 7),
                   rng.randint(0, 7), [{f: rng.randint(0, n.OP_MAX[f]) for f in n.OP_FIELDS} for _ in range(4)])


def same_voice(a: n.Patch, b: n.Patch) -> bool:
    return (a.alg, a.fb, a.ams, a.pms, a.ops) == (b.alg, b.fb, b.ams, b.pms, b.ops)


def ui_hash(p: n.Patch, pan=0, octave=4, poly=1) -> str:
    """What the web app puts after '#patch=': btoa(JSON.stringify(...)); note the tuple orders."""
    payload = {"a": p.alg, "f": p.fb, "p": pan, "o": octave, "y": poly, "l": [p.lfo_freq, p.pms, p.ams],
               "ops": [[o["ar"], o["dr"], o["sr"], o["rr"], o["sl"], o["tl"], o["mul"], o["dt"], o["ks"], o["am"],
                        o["ssgeg"]] for o in p.ops]}
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


class Writer(unittest.TestCase):
    def test_golden_piano(self):
        self.assertEqual(n.build_fui(piano()), PIANO_FUI)

    def test_layout_lengths(self):
        data = n.build_fui(piano())
        self.assertEqual(len(data), 8 + (4 + len("nin8 piano") + 1) + (4 + 37))
        self.assertEqual(data[:4], b"FINS")
        self.assertEqual(struct.unpack_from("<HH", data, 4), (228, 1))
        self.assertEqual(data[8:12], b"NA" + struct.pack("<H", 11))

    def test_operator_order_is_1_3_2_4(self):
        p = piano()
        for i, tl in enumerate((11, 22, 33, 44)):
            p.ops[i]["tl"] = tl
        fm = n.build_fui(p)[8 + 15 + 4:][5:]          # skip header, NA, FM header, 5 base bytes
        self.assertEqual([fm[8 * s + 1] for s in range(4)], [11, 33, 22, 44])

    def test_detune_encoding(self):
        # Furnace stores signed detune + 3; chip codes 5,6,7 are -1,-2,-3 and code 4 is the odd one.
        self.assertEqual(n.DT_REG_TO_FUR, (3, 4, 5, 6, 7, 2, 1, 0))
        for reg in range(8):
            self.assertEqual(n.DT_FUR_TO_REG[n.DT_REG_TO_FUR[reg]], reg)
        self.assertEqual(n.DT_FUR_TO_REG, (7, 6, 5, 0, 1, 2, 3, 4))   # Furnace's own dtTable

    def test_lfo_macro(self):
        macro = lambda v: bytes.fromhex("4d41 0c00 0800 07 01 ff ff 00 00 00 01".replace(" ", "")) + bytes((v, 0xFF))
        self.assertEqual(n.build_fui(piano(ams=1, pms=3, lfo_freq=4))[-16:], macro(4))
        self.assertEqual(n.build_fui(piano(ams=2, lfo_freq=0))[-16:], macro(0))     # speed 0 is still stated
        self.assertEqual(n.build_fui(piano(pms=3, lfo_freq=2))[-16:], macro(2))      # vibrato-only (PMS) counts too
        self.assertEqual(n.build_fui(piano(ams=3, lfo_freq=7))[-16:], macro(7))      # tremolo-only (AMS) as well
        self.assertEqual(n.build_fui(piano(lfo_freq=5)), PIANO_FUI)                  # LFO unused -> no macro
        self.assertEqual(n.build_fui(piano(), lfo_macro="always")[-16:], macro(0))
        never = n.build_fui(piano(ams=1, lfo_freq=4), lfo_macro="never")
        self.assertNotIn(b"MA", never[8:])
        self.assertEqual(never, n.build_fui(piano(ams=1, lfo_freq=0), lfo_macro="never"))

    def test_ams_pms_byte(self):
        fm = n.build_fui(piano(ams=3, pms=5))[8 + 15 + 4:]
        self.assertEqual(fm[2], (3 << 3) | 5)

    def test_version_header_and_limits(self):
        self.assertEqual(n.build_fui(piano(), version=256)[4:6], struct.pack("<H", 256))
        for bad in (223, 257):
            with self.assertRaises(n.PatchError):
                n.build_fui(piano(), version=bad)

    def test_ranges_are_enforced(self):
        for field, bad in (("tl", 128), ("rr", 16), ("dt", 8), ("ssgeg", 16), ("ks", 4), ("am", 2), ("ar", -1)):
            p = piano()
            p.ops[2][field] = bad
            with self.assertRaises(n.PatchError, msg=field):
                n.build_fui(p)
        with self.assertRaises(n.PatchError):
            n.build_fui(piano(alg=8))
        with self.assertRaises(n.PatchError):
            n.build_fui(piano(ops=piano().ops[:3]))

    def test_names(self):
        def name_in(data):
            return n.parse_fui(data)[0].name
        self.assertEqual(name_in(n.build_fui(piano("x" * 70000))), "x" * n.MAX_NAME_BYTES)
        self.assertEqual(name_in(n.build_fui(piano("\U0001F600" * 100))).count("\U0001F600"), n.MAX_NAME_BYTES // 4)
        self.assertEqual(name_in(n.build_fui(piano("a\ud800b"))), "a?b")              # lone surrogate
        self.assertEqual(name_in(n.build_fui(piano("a‮b​c\td"))), "abc d")  # bidi/format chars gone
        self.assertEqual(name_in(n.build_fui(piano("   "))), "Custom")
        self.assertEqual(name_in(n.build_fui(piano("Café ピアノ"))), "Café ピアノ")


class RoundTrip(unittest.TestCase):
    def test_random_patches(self):
        rng = random.Random(98)
        for i in range(500):
            p = random_patch(rng, f"p{i}")
            back, meta = n.parse_fui(n.build_fui(p))
            self.assertTrue(same_voice(p, back), p)
            self.assertEqual(back.name, p.name)
            self.assertEqual(back.lfo_freq, p.lfo_freq if p.uses_lfo else 0)
            self.assertEqual(meta, {"version": 228, "type": 1, "named": True})

    def test_parser_skips_unknown_features_and_stops_at_EN(self):
        blob = bytearray(PIANO_FUI)
        blob += struct.pack("<2sH", b"ZZ", 3) + b"abc"                     # unknown feature
        blob += struct.pack("<2sH", b"EN", 0)
        blob += struct.pack("<2sH", b"NA", 4) + b"bad\x00"                  # after EN: must be ignored
        back, _ = n.parse_fui(bytes(blob))
        self.assertEqual(back.name, "nin8 piano")

    def test_parser_rejects_garbage(self):
        for blob in (b"", b"FUR!....", PIANO_FUI[:30], PIANO_FUI[:4] + struct.pack("<HH", 228, 6)):
            with self.assertRaises(n.PatchError):
                n.parse_fui(blob)

    def test_reads_files_without_name_or_block_byte(self):
        # 49-byte form (no NA), and a pre-224 file: 36-byte FM block with no block byte.
        back, meta = n.parse_fui(PIANO_FUI[:8] + PIANO_FUI[23:])
        self.assertFalse(meta["named"])
        self.assertTrue(same_voice(back, piano()))
        old = struct.pack("<4sHH", b"FINS", 201, 1) + struct.pack("<2sH", b"FM", 36) + PIANO_FUI[8 + 15 + 4:][:4] + PIANO_FUI[8 + 15 + 4 + 5:]
        back, meta = n.parse_fui(old)
        self.assertTrue(same_voice(back, piano()))


class Inputs(unittest.TestCase):
    def test_hash_field_order_with_distinct_values(self):
        """Every field/operator differs, so swapped tuple positions (ks/am/ssgEg, pms/ams) are caught."""
        p = varied()
        got = n.patch_from_hash(ui_hash(p))
        self.assertTrue(same_voice(got, p))
        self.assertEqual((got.lfo_freq, got.pms, got.ams), (6, 5, 2))

    def test_hash_forms(self):
        p = varied()
        b64 = ui_hash(p)
        quoted = urllib.parse.quote(b64)
        for text in (b64, "#patch=" + b64, "https://example.org/nineigth/#patch=" + b64,
                     "x#patch=" + quoted + "&name=Wob%20Bass", b64.rstrip("="),
                     b64.replace("+", "-").replace("/", "_"), "  #patch=" + b64 + "\n",
                     '"https://x/#patch=' + b64 + '"', "'#patch=" + b64 + "'", "<https://x/#patch=" + b64 + ">",
                     "[patch](https://x/#patch=" + b64 + ")", urllib.parse.quote("https://x/#patch=" + b64, safe="")):
            got = n.patch_from_hash(text)
            self.assertTrue(same_voice(got, p), text[:50])
            self.assertEqual(got.lfo_freq, 6)
        self.assertEqual(n.patch_from_hash("#patch=" + b64 + "&name=Wob%20Bass").name, "Wob Bass")
        self.assertEqual(n.patch_from_hash('"#patch=' + b64 + '&name=Wob"').name, "Wob")   # no trailing quote in the name
        self.assertEqual(n.patch_from_hash(b64, name="Mine").name, "Mine")

    def test_long_literal_link_is_not_mistaken_for_a_path(self):
        link = "https://x.example/#patch=" + ui_hash(piano()) + "&name=" + "N" * 300
        self.assertTrue(same_voice(n.load_patches(link)[0], piano()))

    def test_hash_errors(self):
        no_lfo = {"a": 1, "f": 1, "ops": [[0] * 11] * 4}
        for bad in ("", "!!!notbase64!!!", base64.b64encode(b"[1,2]").decode(),
                    base64.b64encode(json.dumps({"a": 9, "f": 0, "l": [0, 0, 0], "ops": [[0] * 11] * 4}).encode()).decode(),
                    base64.b64encode(json.dumps(no_lfo).encode()).decode()):
            with self.assertRaises(n.PatchError, msg=bad[:20]):
                n.patch_from_hash(bad)

    def test_preset_json_variants(self):
        base = varied().to_preset_json()
        self.assertTrue(same_voice(n.patch_from_preset(base), varied()))
        alias = dict(base)
        alias["operators"] = alias.pop("envelopes")
        for e in alias["operators"]:
            e["ssgeg"] = e.pop("ssgEg")
        self.assertTrue(same_voice(n.patch_from_preset(alias), varied()))
        no_lfo = {k: v for k, v in base.items() if k != "lfo"}
        self.assertEqual(n.patch_from_preset(no_lfo).ams, 0)
        broken = json.loads(json.dumps(base))
        del broken["envelopes"][1]["tl"]
        with self.assertRaises(n.PatchError):
            n.patch_from_preset(broken)

    def test_sequence_file_naming_matches_the_ui(self):
        inner = {k: v for k, v in varied("Inner").to_preset_json().items()}
        self.assertEqual(n.patch_from_preset({"name": "Song A", "instrument": inner, "notes": []}).name, "Song A")
        inner.pop("name")
        self.assertEqual(n.patch_from_preset({"instrument": inner}, default_name="file").name, "file")

    def test_booleans_and_floats(self):
        base = varied().to_preset_json()
        base["envelopes"][0]["am"] = True
        with self.assertRaises(n.PatchError):
            n.patch_from_preset(base)
        base["envelopes"][0]["am"] = 1.0                                   # an integral float is fine
        self.assertEqual(n.patch_from_preset(base).ops[0]["am"], 1)
        base["envelopes"][0]["am"] = 0.5
        with self.assertRaises(n.PatchError):
            n.patch_from_preset(base)

    def test_apvts_ids(self):
        params = {"alg": 4, "fb": 5, "lfo_freq": 0, "lfo_ams": 0, "lfo_pms": 0}
        for i, o in enumerate(PIANO_OPS, 1):
            params.update({f"op{i}_{f}": v for f, v in zip(("ar", "dr", "sr", "rr", "sl", "tl", "ks", "mul", "dt", "am", "ssgeg"), o)})
        self.assertEqual(n.build_fui(n.patch_from_params(params, "nin8 piano")), PIANO_FUI)
        del params["op3_tl"]
        with self.assertRaises(n.PatchError):
            n.patch_from_params(params)

    def test_files_and_names(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "one.json").write_text(json.dumps(piano().to_preset_json()))
            (d / "many.json").write_text(json.dumps([piano("a").to_preset_json(), piano("b").to_preset_json()]))
            (d / "link.txt").write_text("copied: https://x/#patch=" + ui_hash(piano()))
            (d / "bom.json").write_text(json.dumps(piano("bom").to_preset_json()), encoding="utf-8-sig")
            nameless = piano().to_preset_json()
            del nameless["name"]
            (d / "my sound.json").write_text(json.dumps(nameless))
            self.assertEqual(n.load_patches(str(d / "one.json"))[0].name, "nin8 piano")
            self.assertEqual([p.name for p in n.load_patches(str(d / "many.json"))], ["a", "b"])
            self.assertEqual(n.load_patches(str(d / "link.txt"))[0].name, "link")        # file stem, like JSON files
            self.assertEqual(n.load_patches(str(d / "bom.json"))[0].name, "bom")
            self.assertEqual(n.load_patches(str(d / "my sound.json"))[0].name, "my sound")

    def test_stdin(self):
        with mock.patch("sys.stdin", io.StringIO(json.dumps(piano("piped").to_preset_json()))):
            self.assertEqual(n.load_patches("-")[0].name, "piped")


class Filenames(unittest.TestCase):
    def test_safe_filename(self):
        self.assertEqual(n.safe_filename('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")
        self.assertNotIn("/", n.safe_filename("../../etc/passwd"))
        self.assertEqual(n.safe_filename(".."), "patch")
        self.assertEqual(n.safe_filename("CON"), "CON_")
        self.assertEqual(n.safe_filename("nul.txt"), "nul.txt_")
        self.assertEqual(n.safe_filename("a‮b"), "ab")
        long_name = n.safe_filename("\U0001F600" * 100)
        self.assertLessEqual(len(long_name.encode()), n.MAX_STEM_BYTES)
        self.assertLessEqual(len((long_name + "-99.fui").encode()), 255)

    def test_unique_stems_even_case_insensitively(self):
        stems = n._unique_stems(["Bell", "Bell", "bell", "a/b", "a_b", "Bell-2"])
        self.assertEqual(len({s.lower() for s in stems}), 6)
        self.assertEqual(stems[0], "Bell")


class Bank(unittest.TestCase):
    def test_bundled_bank(self):
        bank = n.load_bank()
        self.assertEqual(len(bank), 48)
        self.assertEqual(sorted(p.menu for p in bank if p.menu is not None), list(range(1, 32)))
        self.assertEqual(sum(p.menu is None for p in bank), 17)
        for p in bank:
            back, _ = n.parse_fui(n.build_fui(p))
            self.assertTrue(same_voice(p, back), p.name)

    @unittest.skipUnless(n.UI_ZIP.exists(), "ui/webui.zip not present")
    def test_bank_matches_ui_bundle(self):
        """Fails if ui/webui.zip was rebuilt with different presets: run 'nin8fui.py refresh-bank'."""
        committed = json.loads(n.BANK_PATH.read_text())["presets"]
        self.assertEqual(n.bundle_presets_from_zip(), committed)

    def test_extractor_resolves_spread_and_menu(self):
        ops = ",".join(['{ar:1,dr:2,sr:3,sl:4,rr:5,tl:6,mul:7,dt:0,am:1,ssgEg:8,ks:2}'] * 4)
        pre = lambda name, lfo: '{name:"%s",algorithm:1,feedback:2,lfo:%s,pan:3,envelopes:[%s]}' % (name, lfo, ops)
        js = ('var a=1,d={freq:0,ams:0,pms:0};const X=[%s,%s];const Y=[%s,%s,%s];'
              'this.presets=[{name:"Init",algorithm:0,feedback:0,pan:3,lfo:{freq:0,pms:0,ams:0},envelopes:[%s]},...Y.slice(0,2),...X];'
              % (pre("x1", "{...d}"), pre("x2", "{freq:3,ams:1,pms:2}"), pre("y1", "{...d}"), pre("y2", "{...d}"), pre("y3", "{...d}"), ops))
        got = n.extract_bundle_presets(js)
        self.assertEqual([(p["name"], p["menu"]) for p in got], [("y1", 1), ("y2", 2), ("x1", 3), ("x2", 4), ("y3", None)])
        self.assertEqual(got[0]["lfo"], {"freq": 0, "ams": 0, "pms": 0})
        self.assertEqual(got[3]["lfo"], {"freq": 3, "ams": 1, "pms": 2})
        with self.assertRaises(n.PatchError):
            n.extract_bundle_presets(js.replace("d={freq:0,ams:0,pms:0}", "d={freq:3,ams:0,pms:0}"))
        self.assertTrue(all(p["menu"] is None for p in n.extract_bundle_presets(js.replace("presets=[", "presetz=["))))


class Cli(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = n.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_bank_export(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, _ = self.run_cli("bank", d)
            files = sorted(Path(d).glob("*.fui"))
            self.assertEqual((rc, len(files)), (0, 31))                       # only what nin8's menu offers
            self.assertIn("17 more presets", out)
            self.assertEqual([f.name[:2] for f in files], [f"{i:02d}" for i in range(1, 32)])
            self.assertEqual(len({f.name.lower() for f in files}), 31)        # 'Bell' vs 'Bell': numbering keeps them apart
            by_menu = {p.menu: p for p in n.load_bank() if p.menu}
            for f in files:
                self.assertTrue(same_voice(n.parse_fui(f.read_bytes())[0], by_menu[int(f.name[:2])]), f.name)
            self.assertEqual(len(list(Path(d).glob("*.part"))), 0)
        with tempfile.TemporaryDirectory() as d:
            rc, _, _ = self.run_cli("bank", d, "--all")
            names = sorted(f.name for f in Path(d).glob("*.fui"))
            self.assertEqual((rc, len(names), len({x.lower() for x in names})), (0, 48, 48))
            self.assertEqual(names[31][:2], "32")
        with tempfile.TemporaryDirectory() as d:
            rc, _, _ = self.run_cli("bank", d, "--only", "manbow bass", "Sax", "--format-version", "224")
            self.assertEqual((rc, sorted(f.name[:2] for f in Path(d).glob("*.fui"))), (0, ["14", "32"]))
            rc, _, err = self.run_cli("bank", d, "--only", "nope")
            self.assertEqual(rc, 1)
            self.assertIn("no bundled preset", err)

    def test_convert_and_inspect(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            link = "#patch=" + ui_hash(varied()) + "&name=Wob"
            rc, _, _ = self.run_cli("convert", link, "-o", str(d / "w.fui"))
            self.assertEqual(rc, 0)
            rc, out, _ = self.run_cli("inspect", str(d / "w.fui"))
            self.assertIn("'Wob'", out)
            self.assertIn("LFO freq 6 ams 2 pms 5", out)
            rc, out, _ = self.run_cli("inspect", "--json", str(d / "w.fui"))
            self.assertTrue(same_voice(n.patch_from_preset(json.loads(out)), varied()))
            (d / "a.json").write_text(json.dumps(piano("A b").to_preset_json()))
            rc, _, _ = self.run_cli("convert", str(d / "a.json"), link, "-o", str(d / "out"))
            self.assertEqual((rc, sorted(p.name for p in (d / "out").iterdir())), (0, ["A b.fui", "Wob.fui"]))

    def test_colliding_names_never_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, _ = self.run_cli("convert", str(n.BANK_PATH), "-o", d)
            files = list(Path(d).glob("*.fui"))
            self.assertEqual((rc, len(files), len(out.strip().splitlines())), (0, 48, 48))
            self.assertEqual(len({f.name.lower() for f in files}), 48)
            dup = Path(d) / "dups.json"
            dup.write_text(json.dumps([piano("Bell").to_preset_json(), varied("Bell").to_preset_json(), varied("bell").to_preset_json()]))
            rc, _, _ = self.run_cli("convert", str(dup), "-o", str(Path(d) / "sub"))
            self.assertEqual(len(list((Path(d) / "sub").glob("*.fui"))), 3)

    def test_malformed_input_gives_one_line_error(self):
        deep = "[" * 100000
        cases = {
            "envelopes not objects": '{"algorithm":4,"feedback":1,"envelopes":[1,2,3,4]}',
            "lfo is a list": json.dumps(dict(piano().to_preset_json(), lfo=[1, 2, 3])),
            "empty list": "[]", "empty bank": '{"presets":[]}', "list of non-objects": "[1,2]",
            "apvts missing keys": '{"op1_ar":1}', "deep nesting": deep, "huge integer": '{"algorithm":' + "9" * 5000 + "}",
            "garbage text": "this is not a patch at all", "bad link": "#patch=garbage",
        }
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for label, text in cases.items():
                f = d / "in.txt"
                f.write_text(text)
                rc, out, err = self.run_cli("convert", str(f), "-o", str(d / "o.fui"))
                self.assertEqual(rc, 1, f"{label}: {err[:200]}")
                self.assertTrue(err.startswith("nin8fui:") and err.count("\n") == 1, f"{label}: {err[:200]}")
                self.assertFalse((d / "o.fui").exists(), label)
            for label, data in (("binary", bytes(range(256))), ("utf-16", "{}".encode("utf-16"))):
                f = d / "bin.dat"
                f.write_bytes(data)
                rc, _, err = self.run_cli("convert", str(f), "-o", str(d / "o.fui"))
                self.assertEqual(rc, 1, label)
                self.assertTrue(err.startswith("nin8fui:") and err.count("\n") == 1, f"{label}: {err[:200]}")

    def test_missing_file_is_reported_as_such(self):
        rc, _, err = self.run_cli("convert", "/definitely/not/here/preset.json", "-o", "/tmp/never.fui")
        self.assertEqual(rc, 1)
        self.assertIn("no such file", err)
        rc, _, err = self.run_cli("inspect", "/no/such/file.fui")
        self.assertEqual(rc, 1)

    def test_failure_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            good = d / "good.json"
            good.write_text(json.dumps(piano("ok").to_preset_json()))
            rc, _, err = self.run_cli("convert", str(good), "#patch=garbage", "-o", str(d / "out"))
            self.assertEqual(rc, 1)
            self.assertFalse((d / "out").exists())                             # nothing created, not even the directory
            # a bad format version is rejected before any file is written
            rc, _, err = self.run_cli("convert", str(good), "-o", str(d / "x.fui"), "--format-version", "1")
            self.assertEqual(rc, 1)
            self.assertFalse((d / "x.fui").exists())
            # output into a path whose parent is a file: clean error, no .part litter
            rc, _, err = self.run_cli("convert", str(good), "-o", str(good / "sub" / "x.fui"))
            self.assertEqual(rc, 1)
            self.assertTrue(err.startswith("nin8fui:") and err.count("\n") == 1, err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
