# Ethics and publication rules

1. **Flagged for review, never accusations.** The tool highlights behaviour that a trained
   operator may want to look at. Most flags will have innocent explanations (weather,
   machinery problems, waiting for a berth, fishing).
2. **Public outputs contain method and aggregated statistics only.** No identification of
   individual vessels, no state or military vessels, no conclusions about countries.
3. **MMSIs are anonymised** (keyed hash with a secret salt) in all public outputs, figures and
   screenshots.
4. **The Eastern Mediterranean case study is private.** Its data, results and figures live in
   `private/`, which is git-ignored, until the owner decides otherwise.
5. **Data minimisation.** Only the AIS fields needed for the analysis are stored.
