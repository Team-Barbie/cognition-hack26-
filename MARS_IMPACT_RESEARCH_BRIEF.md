# Mars Impact Detective: research and implementation brief for Claude

**Repository:** [Team-Barbie/cognition-hack26-](https://github.com/Team-Barbie/cognition-hack26-)

**Topic:** Fresh impact craters on Mars — single craters versus crater clusters.

**Prepared:** 25 October 2026.

**Status:** Handoff specification. The source workbook was downloaded and inspected; the research, image verification, and implementation described below are still to be performed.

## 1. Start here: instructions for Claude

> Read this entire brief, then implement and carry out this research case study inside the existing Cognition repository. First inspect README.md, PROJECTSPEC.md, INTERFACES.md, the current experiment interfaces, and applicable repository instructions. Use the existing research runtime and evidence/provenance mechanisms where they support the work. Make a bounded plan, obtain the real public data, run the analysis, inspect actual orbital images, and deliver a reproducible report plus a visual evidence viewer. Complete the work rather than stopping at a proposed architecture. Keep the implementation focused on this case study. Preserve unrelated work and use an isolated checkout if the working tree contains an unfinished merge. No new GitHub repository is needed.
>
> The central question is: **Which recent Martian impacts produced a single crater versus a cluster, and do their surrounding blast-zone features differ?** Every displayed numerical result must come from a recorded computation; every image claim must link to identifiable orbital observations. Distinguish published findings, our reproduced results, and any exploratory extension. A null result is a valid result. Do not invent a newly discovered crater, a significant relationship, a precise impact date, an image pair, or an independent verification that was never performed.

## 2. Why this topic fits the project

The audience-facing hook is: **“Something hit Mars between these two photographs. Did it leave one crater or many?”**

A small incoming object can break apart, and one impact event can leave a cluster of craters. Orbital images let a viewer inspect the surface change directly. The research connects planetary geology, atmospheric entry, and impact physics to measurable observations.

| Requirement | Concrete implementation |
| --- | --- |
| Plenty of public data | A published catalog with 1,203 impact sites, paired observation references, and public orbital image archives. |
| Hard science | Impact morphology, fragmentation, surface disturbance, and observational selection effects. |
| Easy to understand | A dated before/after comparison, followed by a close-up of one crater or a cluster. |
| We find something | Reproduce catalog statistics, independently inspect a selected image sample, and test whether comparisons survive specified quality restrictions. |
| Verifiable | Source image IDs, acquisition dates, measurements, code, hashes, exclusions, and uncertainty accompany the findings. |

Use **impact site/event** as the statistical unit. A cluster containing several craters remains one event. “Fresh” here means formation constrained by repeat observations; it does not mean the impact happened this week.

## 3. Research questions and prior knowledge

**Primary analysis:** Compare the presence of halos and linear rays between cataloged single-crater and cluster events. Show group counts, proportions, differences, and uncertainty.

**Visual audit:** For a prespecified sample, can the cataloged morphology and surface change actually be resolved in the available images? Record agreement, disagreement, and insufficient-resolution cases.

**Bounded extension:** Repeat the primary comparison using events with CTX images on both sides of the formation interval, then inspect sensitivity to event diameter and image-quality exclusions. This tests robustness within the observed sample; it does not remove all detection bias.

The 2022 source paper already reports that clusters are common and finds no dependence of the studied albedo features on whether an event formed a single crater or a cluster. Treat that as a result to examine, not a relationship we can claim to have discovered first. The paper also discusses uneven imaging and terrain-dependent detectability. [Published findings and abstract](https://www.usgs.gov/publications/new-craters-mars-updated-catalog).

An original contribution could be a transparent image-quality audit or a documented sensitivity analysis. Establish novelty with a literature check before using the word “new.” Discovering a previously uncataloged impact is an optional later investigation, not a requirement or a promised outcome.

## 4. Public sources to use

| Source | Purpose and access |
| --- | --- |
| [Daubar et al. catalog, Zenodo record 6604912](https://zenodo.org/records/6604912) | Primary tabular dataset. File: `Daubar_2022_catalog_tableS1.xlsx`. Dataset DOI: `10.5281/zenodo.6604912`. |
| [Direct workbook download](https://zenodo.org/records/6604912/files/Daubar_2022_catalog_tableS1.xlsx?download=1) | Small starting download; acquire this before large imagery. |
| [Daubar et al. 2022 paper](https://doi.org/10.1029/2021JE007145) | Interpret columns, feature definitions, event diameter, and study limitations. An [institutional copy is listed here](https://spiral.imperial.ac.uk/entities/publication/1d28065c-142b-42ae-8b64-c2d0ec558fce). |
| [HiRISE](https://hirise.lpl.arizona.edu/) | Resolve catalog observation IDs to detailed image pages, products, labels, and acquisition metadata. |
| [PDS Orbital Data Explorer](https://ode.rsl.wustl.edu/) | Search and download Mars observations across instruments, including CTX and HiRISE. Use the catalog's exact product IDs. |
| [USGS CTX data documentation](https://astrogeology.usgs.gov/docs/concepts/missions/mro/ctx-data/) | Authoritative guidance for retrieving and interpreting CTX products. |
| [Mars Data Browser](https://murray-lab.caltech.edu/Mars-Data-Browser/) | Alternative spatial search for image footprints. A global mosaic is useful for navigation, not a substitute for dated before/after observations. |

Record the source URL, retrieval date, version, citation, and checksum for each acquired file. Preserve source credits; HiRISE requests `NASA/JPL-Caltech/UArizona`. Record the catalog's stated license/terms rather than assuming that all sources share the same license.

This is a **frozen 2022 catalog**, not an up-to-date census of every known Martian impact. Any later datasets must be documented separately and deduplicated before combination.

## 5. Verified workbook audit: do not skip these details

The following facts were obtained by downloading and inspecting the workbook on 25 October 2026. They are ingestion checks, not the completed scientific analysis:

- Download size: **196,534 bytes**.
- MD5 published by Zenodo and matched locally: `4d1b49c2e62785a1b4efb73b2040de4d`. Also calculate SHA-256 for the project's provenance manifest.
- Worksheet: `Sheet1`; **1,203 data rows**, one header row, 20 columns.
- There are **1,203 distinct HiRISE observation IDs** in this version.
- Classification: **503 `single`**, **700 `cluster`**. The cluster fraction is approximately **58.19%**; recompute it from the file.

### Column mapping

The normalized names below are proposed project fields. Retain the original values and row numbers alongside them.

| Excel column | Source header | Proposed interpretation |
| --- | --- | --- |
| A | `HiRISE Observation ID` | `hirise_observation_id`; stable source reference for this frozen catalog. |
| B | `Latitude (deg E, centric)` | `latitude_planetocentric_deg`; header direction suffix appears erroneous. |
| C | `Longitude (deg N)` | `longitude_east_deg`; header direction suffix appears erroneous. |
| D | `Ls of image` | `solar_longitude_deg`; seasonal angle, not geographic longitude. |
| E | `Date of image` | `hirise_acquired_at`; not the impact date. |
| F | `Mars Year of image` | `mars_year`; not an Earth calendar year. |
| G | `Single or cluster` | `event_type`. |
| H | `Ice-exposing impact` | Preserve `y`, `n`, and `possible` separately. |
| I | `Halo` | Normalize letter case, preserve raw value. |
| J | `Linear Rays` | `linear_rays`. |
| K | `Arcuate rays` | Preserve the unresolved code `m` until its meaning is established. |
| L | `Dark-, Light-, or Dual-toned BZ` | Blast-zone tone: `dark`, `light`, or `dual`. |
| M | `Diameter (m)` | `catalog_diameter_m`; verify the paper's event/cluster diameter definition before interpreting it. |
| N–P | Before instrument, ID, date | Source of the last documented pre-impact observation. |
| Q–S | After instrument, ID, date | Source of the documented post-impact observation. |
| T | `Reference for previously published diameter ` | Prior diameter reference; preserve missing values. |

### Parsing traps

1. **Coordinate headers:** B contains values from about −82.03 to +75.42, consistent with latitude; C contains about 3.29 to 359.10, consistent with east longitude. Check several sites against observation metadata before finalizing the interpretation. Correct the misleading suffixes in the normalized schema; do not blindly swap the numeric columns. Image-center coordinates need not exactly equal the catalog's impact coordinates.
2. **Unknown codes:** Ice has 20 `possible` records. Arcuate rays has three `m` records. Halo has one uppercase `Y`. Case normalization is safe; silently converting uncertain codes to `false` is not.
3. **Dates:** Date cells are stored as spreadsheet dates. Parse using the workbook's date system and retain the underlying values. Validate before/after order. Formation is bracketed by observations, not dated exactly.
4. **Different instruments:** The “before,” “after,” and detailed HiRISE images are often three different observations. A HiRISE acquisition date must not replace a CTX bracket date.
5. **Cluster size:** Never label a cluster's catalog diameter as the diameter of its largest visible crater without checking its definition. Do not infer impactor diameter, energy, or fragmentation height from this field alone.
6. **Mars mapping:** Use Mars coordinates and an appropriate planetary projection, or a clearly labeled longitude/latitude plot. An Earth basemap or Earth-specific distance conversion is inappropriate.

## 6. Small, real starter sample

These entries were read from the catalog and selected as retrieval candidates. **Their actual image pairs have not been visually inspected for this brief.** Claude must retrieve and assess them before presenting them as verified evidence.

| Field | Candidate single event | Candidate cluster event |
| --- | --- | --- |
| HiRISE observation | `ESP_046278_2255` | `ESP_037706_1765` |
| Catalog latitude | 45.0611 | −3.4366 |
| Catalog east longitude | 240.5787 | 256.3268 |
| Catalog diameter | 8.5 m | 11.3676 m |
| Before CTX product | `J01_045355_2237_XI_43N119W` | `F02_036572_1765_XN_03S103W` |
| Before date | 2016-03-30 | 2014-05-16 |
| After CTX product | `J02_045632_2276_XI_47N119W` | `F03_036928_1765_XN_03S103W` |
| After date | 2016-04-21 | 2014-06-12 |

Retrieve images by product ID through the archive. Resolve real download links from metadata rather than inventing a directory pattern. If a candidate cannot support a clear comparison, record why and select another using documented criteria.

## 7. What counts as visual verification

Each evidence card should contain:

- The same location before and after, with instrument names, acquisition dates, observation IDs, scale, and orientation.
- A detailed post-impact view with crater rims or cluster members annotated where resolvable.
- A plain-language statement separating visible evidence from interpretation.
- Links to the original products and a record of cropping, projection, registration, and contrast operations.
- A verification status: `verified_surface_change`, `post_impact_morphology_only`, `ambiguous`, or `unusable`, plus an explanation.

Match the geographic footprint using stable surrounding features. Record alignment residuals; inspect them against the size of the feature being compared. Avoid image edges, gaps, saturation, clouds/haze, and materially different shadows. Preserve originals and use reversible image processing.

**Resolution matters:** a before image may show no blast zone while being unable to resolve the eventual individual crater. In that situation say “no corresponding blast zone is visible at this resolution,” not “this image proves there was no crater.” The post-impact HiRISE image supplies the detailed morphology. A dark mark alone is not enough to establish an impact.

Provide a blink comparison or slider only when alignment is good enough to make the comparison meaningful. Otherwise use labeled side-by-side panels and explain the limitation. Do not stretch unrelated views into a fake match. Generated images, generative fill, and inferred super-resolution details are not observational evidence.

The catalog labels and the paper reporting those labels are not two independent observations. A fresh image inspection is an additional check; describe who or what performed it, whether catalog labels were hidden, and any remaining dependence. Never describe an automated review as a human review.

## 8. Analysis plan

### A. Reproduce the catalog baseline

Download and hash the workbook, normalize its fields, and export a schema/data-quality report. Recompute the event counts above. List exclusions and denominators for every analysis; missing or ambiguous values must remain visible.

### B. Compare single events and clusters

For halo presence and linear-ray presence, calculate each group's numerator, denominator, proportion, and the absolute difference in proportions. Include 95% uncertainty intervals, for example Wilson intervals for proportions and a seeded bootstrap interval for the difference. Keep the two comparisons prespecified; label any extra feature searches exploratory and account for multiple comparisons when making inferential claims.

Use contingency tables and an appropriate test where useful, but lead with effect sizes and uncertainty. A non-significant result is not proof of identical populations. These intervals describe the observed catalog under the stated statistical assumptions; they do not correct for how impacts were detected.

### C. Check robustness

Repeat the comparison for CTX-before/CTX-after events and within documented diameter strata with adequate group sizes. As a sensitivity check, show what changes when excluding events below 8 m, while explicitly avoiding any claim that this alone makes the sample complete. Report when a restriction leaves too few observations for a useful comparison.

Do not infer impact frequency from a density map or compare impact rates between regions without accounting for searched area, observation duration, resolution, and repeat coverage. Detection depends on imagery and surface visibility as well as impacts.

### D. Audit images

First prove the retrieval and comparison workflow on one single event and one cluster. Then build at least **six usable evidence cards**, including at least three of each type. For a broader audit, target 20 sites using a recorded selection method and seed, with replacements and failures logged.

The curated demo cards are illustrative, not a random population sample. A balanced audit sample cannot estimate the catalog-wide cluster fraction. If reviewing labels, annotate the images before revealing the catalog class where feasible; record unresolved small craters rather than guessing.

### E. Produce the result

Report the reproduced baseline, feature comparisons, sensitivity results, and visual audit separately. Explain any disagreement with prior work. An honest “no clear difference detected in this sample” with reproducible evidence satisfies the research objective.

## 9. Build this within Cognition

Use a case-study directory such as `research/mars_impacts/` with the existing runtime's `input/` and `output/` conventions. Inspect current interfaces before finalizing paths.

Suggested contents:

```text
research/mars_impacts/
  README.md                     # Setup, actual commands, expected outputs
  input/
    brief.md                    # Executable research scope derived from this document
    context.md                  # Definitions, prior findings, assumptions
    sources.json                # Source versions, URLs, checksums, citations
    papers/                     # Attributed source material usable by the evidence reader
    datasets/                   # Normalized catalog and documented small inputs
    code/                       # Retrieval, normalization, analysis, figure generation
    experiment.yaml             # Declared computation matching the current runtime schema
  data/raw/                     # Download cache; large imagery excluded from Git
  output/
    report.md
    metrics.json
    data-quality.json
    image-manifest.json
    image-audit.csv
    figures/
    evidence-viewer.html
    provenance.json
```

Requirements for implementation:

- Use Python for data processing; add only necessary dependencies, preferably as an optional research extra. Ordinary Python tools such as pandas/openpyxl, NumPy/SciPy, and plotting/image libraries are sufficient for the tabular stage. Choose planetary image tooling after inspecting actual product formats.
- Separate network acquisition from reproducible analysis. Cache downloads, record checksums, and support rerunning analysis from cached inputs. Do not bulk-download the planetary archive.
- Connect computation to the repository's declared `ExperimentSpec`/`ExperimentRun` mechanism after checking current schemas. Capture inputs, commands, seeds, outputs, and failures.
- Preserve the runtime's distinction between `runtime_captured` and `host_reported` work. If a capability is unavailable, record that limitation and produce a reproducible standalone result; do not relabel outside execution as managed execution.
- Add source material through the established evidence/import path. Preserve citations and source spans; generated summaries are not independent external evidence.
- Provide a lightweight HTML viewer with selectable sites, image comparisons, annotations, measurement tables, and source links. Integrate through the existing Studio if practical; avoid rebuilding the application for this case study.
- Show real data only. If fetching or processing a site fails, show the status and reason rather than substituting synthetic results. Do not present the runtime's fake-agent fixture as a live Mars investigation.
- Keep original images intact. Commit small code, manifests, and appropriate derived artifacts; fetch large originals by script. Follow repository policy for data attribution and distribution.

The viewer's opening screen should answer three things immediately: **Where did an impact occur? What changed? How can I check the evidence?** A plot of catalog sites, a single/cluster comparison, and clickable evidence cards are enough; a decorative 3D Mars globe is optional.

## 10. Delivery order and acceptance criteria

1. **Data foundation:** Validate the frozen workbook, resolve parsing issues, and produce counts plus a data-quality report.
2. **First evidence:** Resolve two real image pairs and prove the alignment/annotation workflow before building the full viewer.
3. **Analysis:** Run the prespecified comparisons and sensitivity checks; export machine-readable results and figures.
4. **Research deliverable:** Complete at least six usable cards, the report, provenance, and a working viewer.
5. **Reproduction:** Run the same analysis again from frozen inputs and verify matching analytical outputs. Check meaningful parser, missing-data, date-bracketing, coordinate, and denominator behavior. Verify displayed site links and the viewer in a browser.

The handoff is complete when:

- [ ] The frozen source reproduces 1,203 sites, 503 singles, and 700 clusters, or a clearly explained version change accounts for the difference.
- [ ] Misleading coordinate suffixes and uncertain feature codes are explicitly handled.
- [ ] The report answers the primary question with computed effect sizes, uncertainty, and limitations.
- [ ] At least six image cards pass the documented visual checks, with at least three single and three cluster events.
- [ ] Every card exposes product IDs, dates, source links, and processing history.
- [ ] Numerical claims can be traced to input rows and recorded computation; image claims can be traced to real observations.
- [ ] The viewer displays the actual analysis outputs and clearly marks unavailable or ambiguous evidence.
- [ ] A teammate can follow the README from a clean environment and reproduce the analysis from the specified data.
- [ ] The final summary separates completed work from unresolved image access, interpretation, or integration problems.

## 11. Suggested final presentation

Open with one verified before/after site. Let the viewer see the new surface disturbance, then zoom into a cluster and explain that one event can leave multiple craters. Show the catalog-scale comparison next, including a null result if that is what the data support. End by opening the source observation and the computation behind one displayed number.

The intended payoff is a research result that a viewer can inspect and another researcher can reproduce—not a promise that an automated system has discovered an unknown impact.
