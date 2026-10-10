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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nin8fui as n  # noqa: E402

# nin8's C++ default patch (src/engine/FMPatch.h kDefaultEnvelopes, alg 4 / fb 5):
#                ar dr sr rr sl tl ks mul dt am ssgeg
PIANO_OPS = [(31, 8, 0, 7, 12, 20, 0, 2, 7, 0, 0), (31, 5, 2, 7, 6, 4, 0, 2, 3, 0, 0),
             (31, 7, 4, 7, 10, 18, 0, 2, 3, 0, 0), (31, 0, 4, 7, 0, 0, 0, 2, 0, 0, 0)]
# Golden file: the piano above as Furnace reads it (operators in 1,3,2,4 order, detune remapped, kvs=2).
PIANO_FUI = bytes.fromhex(
    "46494e53 e400 0100 4e41 0b00 6e696e3820 7069616e6f 00 464d 2500 f4 45 00 00 00"
    "02141f08 40c70000 62121f07 44a70000 62041f05 42670000 32001f00 44070000".replace(" ", ""))


def piano(name="nin8 piano", **over) -> n.Patch:
    ops = [dict(zip(n.OP_FIELDS, o)) for o in PIANO_OPS]
    p = n.Patch(name, 4, 5, 0, 0, 0, ops)
    for k, v in over.items():
        setattr(p, k, v)
    return p


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

    def test_golden_without_name_block_is_49_bytes(self):
        self.assertEqual(len(PIANO_FUI) - 15, 49)     # the 'NA' feature is 4 + 11 bytes

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
    def test_hash_forms(self):
        p = piano(ams=2, pms=5, lfo_freq=6)                      # asymmetric LFO values catch order mix-ups
        b64 = ui_hash(p)
        for text in (b64, "#patch=" + b64, "https://example.org/nineigth/#patch=" + b64,
                     "x#patch=" + urllib.parse.quote(b64) + "&name=Wob%20Bass",
                     b64.rstrip("="), b64.replace("+", "-").replace("/", "_")):
            got = n.patch_from_hash(text)
            self.assertTrue(same_voice(got, p), text[:40])
            self.assertEqual(got.lfo_freq, 6)
        self.assertEqual(n.patch_from_hash("#patch=" + b64 + "&name=Wob%20Bass").name, "Wob Bass")
        self.assertEqual(n.patch_from_hash(b64, name="Mine").name, "Mine")

    def test_long_literal_link_is_not_mistaken_for_a_path(self):
        link = "https://x.example/#patch=" + ui_hash(piano()) + "&name=" + "N" * 300
        self.assertTrue(same_voice(n.load_patches(link)[0], piano()))

    def test_hash_errors(self):
        for bad in ("", "!!!notbase64!!!", base64.b64encode(b"[1,2]").decode(),
                    base64.b64encode(json.dumps({"a": 9, "f": 0, "l": [0, 0, 0], "ops": [[0] * 11] * 4}).encode()).decode()):
            with self.assertRaises(n.PatchError, msg=bad[:20]):
                n.patch_from_hash(bad)

    def test_preset_json_variants(self):
        base = piano().to_preset_json()
        self.assertTrue(same_voice(n.patch_from_preset(base), piano()))
        alias = dict(base)
        alias["operators"] = alias.pop("envelopes")
        for e in alias["operators"]:
            e["ssgeg"] = e.pop("ssgEg")
        self.assertTrue(same_voice(n.patch_from_preset(alias), piano()))
        seq = {"name": "song", "instrument": {k: v for k, v in base.items() if k != "name"}, "notes": []}
        self.assertEqual(n.patch_from_preset(seq).name, "song")
        no_lfo = {k: v for k, v in base.items() if k != "lfo"}
        self.assertEqual(n.patch_from_preset(no_lfo).ams, 0)
        broken = json.loads(json.dumps(base))
        del broken["envelopes"][1]["tl"]
        with self.assertRaises(n.PatchError):
            n.patch_from_preset(broken)

    def test_apvts_ids(self):
        params = {"alg": 4, "fb": 5, "lfo_freq": 0, "lfo_ams": 0, "lfo_pms": 0}
        for i, o in enumerate(PIANO_OPS, 1):
            params.update({f"op{i}_{f}": v for f, v in zip(("ar", "dr", "sr", "rr", "sl", "tl", "ks", "mul", "dt", "am", "ssgeg"), o)})
        self.assertEqual(n.build_fui(n.patch_from_params(params, "nin8 piano")), PIANO_FUI)

    def test_files(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "one.json").write_text(json.dumps(piano().to_preset_json()))
            (d / "many.json").write_text(json.dumps([piano("a").to_preset_json(), piano("b").to_preset_json()]))
            (d / "link.txt").write_text("copied: https://x/#patch=" + ui_hash(piano()))
            self.assertEqual(n.load_patches(str(d / "one.json"))[0].name, "nin8 piano")
            self.assertEqual([p.name for p in n.load_patches(str(d / "many.json"))], ["a", "b"])
            self.assertEqual(n.load_patches(str(d / "link.txt"))[0].name, "Custom")


class Bank(unittest.TestCase):
    def test_bundled_bank(self):
        bank = n.load_bank()
        self.assertEqual(len(bank), 48)
        for p in bank:
            back, _ = n.parse_fui(n.build_fui(p))
            self.assertTrue(same_voice(p, back), p.name)

    @unittest.skipUnless(n.UI_ZIP.exists(), "ui/webui.zip not present")
    def test_bank_matches_ui_bundle(self):
        """Fails if ui/webui.zip was rebuilt with different presets: run 'nin8fui.py refresh-bank'."""
        committed = json.loads(n.BANK_PATH.read_text())["presets"]
        self.assertEqual(n.bundle_presets_from_zip(), committed)

    def test_extractor_resolves_the_lfo_spread(self):
        js = ('var a=1,d={freq:0,ams:0,pms:0};const nt=[{name:"X",algorithm:1,feedback:2,lfo:{...d},pan:3,envelopes:['
              + ",".join(['{ar:1,dr:2,sr:3,sl:4,rr:5,tl:6,mul:7,dt:0,am:1,ssgEg:8,ks:2}'] * 4) + ']}];')
        self.assertEqual(n.extract_bundle_presets(js)[0]["lfo"], {"freq": 0, "ams": 0, "pms": 0})
        with self.assertRaises(n.PatchError):
            n.extract_bundle_presets(js.replace("d={freq:0,ams:0,pms:0}", "d={freq:3,ams:0,pms:0}"))


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
            self.assertEqual((rc, len(files)), (0, 48))
            self.assertEqual(len({f.name.lower() for f in files}), 48)       # 'Bell' appears twice: numbering keeps them apart
            for f, p in zip(files, n.load_bank()):
                self.assertTrue(same_voice(n.parse_fui(f.read_bytes())[0], p), f.name)
            rc, _, _ = self.run_cli("bank", d, "--only", "manbow bass", "--format-version", "224")
            self.assertEqual(rc, 0)
            rc, _, err = self.run_cli("bank", d, "--only", "nope")
            self.assertEqual(rc, 1)
            self.assertIn("no bundled preset", err)

    def test_convert_and_inspect(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            link = "#patch=" + ui_hash(piano(ams=1, pms=2, lfo_freq=3)) + "&name=Wob"
            rc, _, _ = self.run_cli("convert", link, "-o", str(d / "w.fui"))
            self.assertEqual(rc, 0)
            rc, out, _ = self.run_cli("inspect", str(d / "w.fui"))
            self.assertIn("'Wob'", out)
            self.assertIn("LFO freq 3 ams 1 pms 2", out)
            rc, out, _ = self.run_cli("inspect", "--json", str(d / "w.fui"))
            self.assertTrue(same_voice(n.patch_from_preset(json.loads(out)), piano(ams=1, pms=2)))
            # several inputs -> directory, names taken from the patches
            (d / "a.json").write_text(json.dumps(piano("A b").to_preset_json()))
            rc, _, _ = self.run_cli("convert", str(d / "a.json"), link, "-o", str(d / "out"))
            self.assertEqual((rc, sorted(p.name for p in (d / "out").iterdir())), (0, ["A b.fui", "Wob.fui"]))

    def test_errors_are_one_line_and_exit_1(self):
        rc, _, err = self.run_cli("convert", "#patch=garbage", "-o", "/nonexistent-dir-xyz/out.fui")
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("nin8fui:") and err.count("\n") == 1, err)
        rc, _, err = self.run_cli("inspect", "/no/such/file.fui")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
