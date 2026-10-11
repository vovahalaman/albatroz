## Batch 1 review (verified: true — confirmed by Albatroz distance feedback)

### Scoring strategy
- **Class vs. instance evidence.** If the top hypothesis rests on a landmark *class*
  (a castle, a bridge over still water, an estuary with port cranes, a granite village),
  also record the next most likely *other instances* of that class in the region as separate
  hypotheses with honest (low) weights, and keep the class-level guess at ≤ 0.6. The code decides the
  10 slots; the alternatives let the system ask the human a multiple-choice question, which costs only 5 %.
- **Overconfidence.** Hypotheses weighted 0.95 were wrong about 1 time in 5; every hypothesis below 0.8
  was wrong. Reserve > 0.9 for a unique clue confirmed at the point by an independent mapped feature.
- **Urban + region-only knowledge = near-zero score.** At d=30 km, θ_U ≈ 0.006.
  For urban panoramas never record a region or admin centroid as the answer. Enumerate 3–5 distinct
  concrete candidate sites (villages, squares, POIs) via overpass/geocode, each as its own hypothesis.

### Geocoding hygiene
- **Institutions have several sites.** A geocoded organisation name often resolves to its
  headquarters, not the building in the image. Query all matches (overpass_named) and pick the one
  whose surroundings match the observed architecture (e.g. Pombaline azulejo façades in narrow
  cobbled lanes ≠ waterfront warehouse district).
- **Linear features are ambiguous along their length.** Railway cuttings, rivers and avenues have
  many near-identical spots (several overpasses over the same line). Pin the position with a second,
  unique landmark (tower, chimney, church) and its bearing, not with the linear feature alone.

### Time
- The filename timestamp is not a precise capture time (it can be off by minutes).
  Treat sun-position checks as weak evidence only.
