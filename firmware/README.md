# Firmware

This directory is intentionally empty. The C961 calibration display firmware is
copyrighted by Bafang and is **not** distributed with this project; `.hex`,
`.bin` and `*c961*.txt` files are git-ignored so a copy cannot be committed by
accident.

The analysis in [`docs/firmware-analysis.md`](../docs/firmware-analysis.md) was
made from this image:

| File | SHA-256 |
|------|---------|
| `c961_display_g510_cal` (Intel HEX) | `a67025f73719a0f2e6b796a09c9b0341eb80ac9f34e1e1506e0c442fee3ceffc` |

If you have a copy (or a different version), you can check it against the tool:

```console
python tools/analyze_firmware.py firmware/c961_display_g510_cal.txt
```

The script reports whether every protocol constant used by `bafang-cal` is
present in the image. Results for other firmware versions are very welcome —
please open an issue with the script's output (not the firmware itself).
