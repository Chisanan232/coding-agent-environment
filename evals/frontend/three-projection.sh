#!/usr/bin/env bash
# Verify the camera-only Three.js snapshot before allowing Node to read its modules.
set -euo pipefail
verify_only=false
if [[ ${1:-} == --verify-only ]]; then
  verify_only=true
  shift
fi
if [[ $# != 1 ]]; then
  echo "Usage: bash evals/frontend/three-projection.sh [--verify-only] EXPORTED_CAMERA_TREE" >&2
  exit 2
fi
repo=$(cd -- "$(dirname -- "$0")/../.." && pwd -P)
snapshot=$(cd -- "$1" && pwd -P)
digest=$(python3 -B "$repo/scripts/visual-skills.py" --print-digest "$snapshot")
if [[ "$digest" != d8c2b91d43e4ad8a961d651edc4747aac18f2f8042a3819279cd96e69a50da77 ]]; then
  echo "Unexpected Three.js camera tree digest" >&2
  exit 1
fi

if [[ "$verify_only" == true ]]; then
  echo "$digest"
  exit 0
fi

# Node 26 supports the network permission scope. Only this snapshot is readable.
env -i PATH="$PATH" node --permission --allow-fs-read="$snapshot" --input-type=module - "$snapshot" <<'JS'
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

assert.equal(process.permission.has('net'), false);
assert.equal(process.permission.has('child'), false);
const root = process.argv[2];
const load = path => import(pathToFileURL(join(root, path)).href);
const { PerspectiveCamera } = await load('src/cameras/PerspectiveCamera.js');
const { Object3D } = await load('src/core/Object3D.js');
const { Vector3 } = await load('src/math/Vector3.js');
const { REVISION } = await load('src/constants.js');
assert.equal(REVISION, '187');

const scene = new Object3D();
const box = new Object3D();
scene.add(box);
box.scale.set(2, 1, 1);
const camera = new PerspectiveCamera(90, 16 / 9, 0.1, 100);
camera.position.z = 5;
scene.add(camera);
scene.updateMatrixWorld(true);

function projectedWidth() {
  const xs = [];
  for (const x of [-0.5, 0.5]) {
    for (const y of [-0.5, 0.5]) {
      for (const z of [-0.5, 0.5]) {
        const vertex = new Vector3(x, y, z);
        box.localToWorld(vertex).project(camera);
        assert.ok(vertex.toArray().every(n => Number.isFinite(n) && Math.abs(n) <= 1));
        xs.push(vertex.x);
      }
    }
  }
  return Math.max(...xs) - Math.min(...xs);
}

const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `expected ${a} to equal ${b}`);
const landscape = projectedWidth();
close(landscape, 2 / (4.5 * (16 / 9)));
camera.aspect = 9 / 16;
camera.updateProjectionMatrix();
const portrait = projectedWidth();
close(portrait, 2 / (4.5 * (9 / 16)));
box.rotation.y = Math.PI / 2;
scene.updateMatrixWorld(true);
const turned = projectedWidth();
close(turned, 1 / (4 * (9 / 16)));

console.log(JSON.stringify({ revision: REVISION, corners: 8, landscape, portrait, turned }));
JS
