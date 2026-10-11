## Batch 1 review (verified: true — confirmed by Albatroz distance feedback)

### Scoring strategy
- **Class vs. instance evidence.** If the top hypothesis rests on a landmark *class*
  (a castle, a bridge over still water, an estuary with port cranes, a granite village),
  record the next most likely *other instances* of that class in the region as separate hypotheses
  with honest weights. Never let one class-level guess carry all the weight.
  (Ranks are not ours to fill: the Strategist puts the single best point in all 10 slots, and weights are
  normalised. An honest twin is what lets the system ask the human in assisted runs, and it moves the
  expected-best point in non_urban.)
- **Urban + region-only knowledge = near-zero score.** At d=30 km, θ_U ≈ 0.006.
  For urban panoramas never submit a region or admin centroid. Enumerate ≥5 distinct concrete
  candidate sites (villages, squares, POIs) via overpass and record them as separate hypotheses.

### Geocoding hygiene
- **Institutions have several sites.** A geocoded organisation name often resolves to its
  headquarters, not the building in the image. Query all matches (overpass_named) and pick the one
  whose surroundings match the observed architecture (e.g. Pombaline azulejo façades in narrow
  cobbled lanes ≠ waterfront warehouse district).
- **Linear features are ambiguous along their length.** Railway cuttings, rivers and avenues have
  many near-identical spots (several overpasses over the same line). Pin the position with a second,
  unique landmark (tower, chimney, church) and its bearing, not with the linear feature alone.

### Time
- The filename timestamp is not a precise capture time (identical stamps were seen ~200 m apart).
  Treat sun-position checks as weak evidence only. (Coarse directions from the sun compass — which side
  the water or a landmark is on — stay usable with ±30° tolerance.)
