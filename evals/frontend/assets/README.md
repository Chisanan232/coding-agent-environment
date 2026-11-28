# Original asset probes for B12 and B13

These fixtures are original synthetic inputs. Each has a small, inspectable source and a bounded test. Keep generated images, videos, model packages and run logs outside the checkout.

## Procedural illustration, compositing and loop

Run the Python 3.9+ script with an existing FFmpeg executable and local image converter. Choose a new output directory. The measured configuration used macOS `sips` and Playwright's local FFmpeg n7.0.1-build-1013. The alternative ImageMagick 7 command path is available for other hosts and has not been verified on the audit machine.

```sh
python3 evals/frontend/assets/orbit.py "$run_dir/orbit" --ffmpeg "$ffmpeg_binary" --converter sips
```

The generator combines four editable SVG layers, writes eight frames, compresses them to VP9 and decodes them again. The threshold mask selects the amber orb. Compare each decoded displacement vector with the ellipse's analytical step, including the last-to-first step. A run fails at 1 px error or more. Inspect the eight decoded PNGs in order for edges, color and visible motion. Encoder metadata, rendering and color management can vary across tool versions, so a portable pass uses measured positions and dimensions. This test exercises a known trajectory. B12's seeded browser field and B13's UI interaction critique require their own evidence.

## Geometry, materials, camera and rig

`wheel.usda` is an editable octagonal mesh with 16 indexed vertices, two caps and eight side faces, a bound preview material, one sphere light and three camera position keys. `flag.usda` is an original two-joint, six-vertex rig with three keyed tip rotations. Inspect these plain-text sources directly. Run the following with preinstalled OpenUSD tools, from the repository root, using a new private directory.

```sh
asset_dir="$PWD/evals/frontend/assets"
mkdir "$run_dir/usd" && (
  cd "$run_dir/usd" || exit 1
  usdcat "$asset_dir/wheel.usda" -o wheel.usdc &&
    usdchecker wheel.usdc &&
    usdzip wheel.usdz wheel.usdc &&
    usdchecker wheel.usdz &&
    usdcat "$asset_dir/flag.usda" -o flag.usdc &&
    usdchecker flag.usdc &&
    usdcat flag.usdc -o flag.roundtrip.usda
)
```

In the round-trip, verify the skeleton relationship, per-vertex weights, tip rotation samples and joint hierarchy survive. A 45° positive rotation about the joint at (0, 1) maps the upper vertex (0.2, 2) to approximately (−0.5657, 1.8485). This is a reference deformation calculation. Structural validation alone does not show how a particular renderer skins, lights or frames the asset. Record the runtime and rendered evidence separately if those properties are being graded.
