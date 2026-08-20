# t2i-annotation

Static NegGenEval annotation site for GitHub Pages.

## Files
- `/home/runner/work/t2i-annotation/t2i-annotation/index.html`
- `/home/runner/work/t2i-annotation/t2i-annotation/style.css`
- `/home/runner/work/t2i-annotation/t2i-annotation/app.js`

## Firebase setup
1. Create a Firestore project.
2. Replace `REPLACE_WITH_*` Firebase values in `app.js` (or define `window.NEGGENEVAL_CONFIG` before loading `app.js`).
3. Provide collections:
   - `datapoints` (each document stores prompt/image/objects/conditions)
   - `annotators`
   - `assignments`
   - `annotations`
