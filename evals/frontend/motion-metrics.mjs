// Score real browser samples against the expected physical movement.
function quantile(values, p) {
  const sorted = values.slice().sort((a,b) => a-b);
  return sorted[Math.floor((sorted.length-1)*p)];
}

export function analyzeFrames(series, id, reduced, springTravel) {
  if (series.length < 3) return { pass: false, reason: 'insufficient frames' };
  const gaps = series.slice(1).map((s,i) => s.ms - series[i].ms).filter(n => n > 0);
  const first = series[0].value, last = series.at(-1).value;
  const levels = new Set(series.map(s => s.value.toFixed(3))).size;
  const result = { frames: series.length, medianFrameMs: quantile(gaps,.5), p95FrameMs: quantile(gaps,.95), levels };
  if (id === 'B09') {
    const travel = last - first;
    if (!(springTravel > 0 && travel > 0 && Math.abs(travel - springTravel) <= Math.max(1, springTravel * .02))) {
      return { ...result, pass: false, reason: 'spring did not travel forward to the requested anchor' };
    }
  }
  if (id === 'B12') return { ...result, pass: levels === 2 };
  if (id === 'B07' && reduced) return { ...result, pass: levels === 1 };
  if (reduced) return { ...result, pass: levels === 2 && first !== last };
  result.pass = levels >= 3 && first !== last;
  if (id === 'B09') {
    const progress = series.map(s => (s.value-first)/(last-first));
    const lastOutside = progress.findLastIndex(x => Math.abs(1-x) > .02);
    result.overshoot = Math.max(...progress) - 1;
    result.settledWithin2pctMs = series[lastOutside + 1]?.ms ?? null;
    const velocities = series.slice(1).map((s,i) => (s.value-series[i].value)*1000/(s.ms-series[i].ms))
      .filter(Number.isFinite);
    result.peakForwardPxPerSec = Math.max(...velocities);
    result.peakReturnPxPerSec = Math.min(...velocities);
    result.pass &&= result.overshoot > .02 && result.overshoot < .55 && result.peakReturnPxPerSec < 0
      && result.settledWithin2pctMs !== null && result.settledWithin2pctMs < 1450;
  }
  return result;
}
