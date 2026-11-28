import assert from 'node:assert/strict';
import test from 'node:test';
import { analyzeFrames } from './motion-metrics.mjs';

function sample(positions) {
  return positions.map((value,i) => ({ value, ms:i*80 }));
}

test('B09 rejects backward motion under both motion preferences', () => {
  assert.equal(analyzeFrames(sample([100,70,0,-13,-3,0]), 'B09',false,100).pass,false);
  assert.equal(analyzeFrames(sample([100,0,0]), 'B09',true,100).pass,false);
});

test('B09 checks anchor distance, overshoot and reduced-motion endpoint', () => {
  assert.equal(analyzeFrames(sample([0,2,4,5,4]), 'B09',false,100).pass,false);
  assert.equal(analyzeFrames(sample([0,55,91,100]), 'B09',false,100).pass,false);
  assert.equal(analyzeFrames(sample([0,60,105,112,102,100,100]), 'B09',false,100).pass,true);
  assert.equal(analyzeFrames(sample([0,100,100]), 'B09',true,100).pass,true);
});
