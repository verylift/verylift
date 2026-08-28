// Shared y/x-axis config for Chart.js scales that plot points (an integer
// by construction — see PointEarnEvent.points_earned). Chart.js's default
// "nice numbers" tick algorithm happily picks fractional steps on small
// ranges (e.g. 1.2-point gridlines when the leader has 6 points), which the
// domain can never produce. This picks a whole-number step size that scales
// with the data's max value, so a ~5-point range gets 1-point gridlines and
// a ~300-point range doesn't try to draw 300 of them.
(function (global) {
  function niceIntegerStepSize(maxValue, targetTicks) {
    var safeMax = isFinite(maxValue) && maxValue > 0 ? maxValue : 1;
    var rawStep = safeMax / (targetTicks || 8);
    var magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
    var residual = rawStep / magnitude;
    // Epsilon so float error can't push an exact boundary into the next
    // bracket -- a rawStep that should give residual 5 landing at
    // 5.000000000000001 would otherwise pick a step of 10 and halve the
    // gridline density.
    var epsilon = 1e-9;
    var niceResidual =
      residual <= 1 + epsilon
        ? 1
        : residual <= 2 + epsilon
          ? 2
          : residual <= 5 + epsilon
            ? 5
            : 10;
    return Math.max(1, Math.round(niceResidual * magnitude));
  }

  function maxOfDatasets(datasets) {
    var max = 0;
    (datasets || []).forEach(function (ds) {
      (ds.data || []).forEach(function (value) {
        if (typeof value === "number" && value > max) max = value;
      });
    });
    return max;
  }

  global.integerPointsAxisTicks = function (datasets, targetTicks) {
    return { stepSize: niceIntegerStepSize(maxOfDatasets(datasets), targetTicks), precision: 0 };
  };
})(window);
