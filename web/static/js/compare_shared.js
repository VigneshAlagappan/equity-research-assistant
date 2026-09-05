/* Small shared state between the Compare page's two tabs (web/static/js/
   compare.js's Quick Comparison, web/static/js/compare_detailed.js's
   Detailed Comparison) -- loaded before both, so either can reference it
   regardless of which one's own DOMContentLoaded handler runs first.

   Two things live here, both because duplicating them per-tab would mean
   two copies that could disagree with each other:

   1. Quick Comparison's company selection, published here so Detailed
      Comparison starts from the same companies instead of asking the user
      to pick them twice -- "no need to separately select" was the
      explicit ask. One-directional (Quick -> Detailed) by design: Quick
      Comparison's 4 slots are the more curated, at-a-glance set; Detailed
      Comparison can still add its own extra companies on top (up to the
      same 4-company cap) for a deeper look without those extras leaking
      back and reshuffling Quick Comparison's slots.

   2. The USD/INR spot rate (web/fx_rate.py's /fx/usdinr.json) -- fetched
      at most once per page load and shared, so a session that uses both
      tabs' cross-currency conversion doesn't hit the endpoint twice for
      the same rate. */
(function () {
  "use strict";

  let quickCompanies = []; // ordered [{id, name}], compacted (no gaps)
  const quickListeners = [];

  function getQuickCompanies() {
    return quickCompanies.slice();
  }

  function setQuickCompanies(list) {
    quickCompanies = list.slice();
    quickListeners.forEach((fn) => fn(quickCompanies.slice()));
  }

  function subscribeQuickCompanies(fn) {
    quickListeners.push(fn);
  }

  let fxPromise = null;
  function getUsdInrRate() {
    if (!fxPromise) {
      fxPromise = fetch("/fx/usdinr.json")
        .then((r) => (r.ok ? r.json() : { rate: null }))
        .catch(() => ({ rate: null }));
    }
    return fxPromise;
  }

  window.CompareShared = {
    getQuickCompanies: getQuickCompanies,
    setQuickCompanies: setQuickCompanies,
    subscribeQuickCompanies: subscribeQuickCompanies,
    getUsdInrRate: getUsdInrRate,
  };
})();
