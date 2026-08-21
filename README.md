# t2i-annotation

Static NegGenEval annotation site for GitHub Pages.

## Files
- `index.html`
- `style.css`
- `js/` — ES modules (entry point: `js/main.js`); see `js/config.js` for the
  Firebase Web configuration
- `src/` — Python scripts for uploading datapoints and downloading
  annotations; see `src/SETUP.md`

## Firebase setup
1. Create a Firestore project.
2. Set the Firebase Web configuration in `js/config.js`.
3. Provide collections:
   - `datapoints` (each document stores prompt/image/objects/conditions)
   - `annotators`
   - `assignments`
   - `annotations`
