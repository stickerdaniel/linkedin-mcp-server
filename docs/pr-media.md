# Screenshots for rendered changes

A pull request that changes a rendered page shows a before and an after screenshot of every changed section in its description. Rendered pages are any `README.md` (GitHub and PyPI), `docs/docker-hub.md` (Docker Hub) and the `long_description` in `manifest.json` (Claude Desktop). The README media check enforces this.

1. **Capture.** Take the before shot from the file's GitHub page on the base branch and the after shot from the same page on your branch: same section, same width. Take one pair per changed section. Any browser or screenshot tool works. For `manifest.json`, show the `long_description` rendered as Markdown, not the JSON source.
2. **Look.** Open each image. The changed text is readable in the after shot. Two identical images mean the shot missed the change.
3. **Place.** Put a table above the attribution line of the description:

   ```markdown
   | Before | After |
   | --- | --- |
   | ![Before](before.png) | ![After](after.png) |
   ```

4. **Upload** the images as GitHub attachments, which stay visible after the branch is deleted. Use one route:
   - In the browser, edit the description and drag the images into the table cells.
   - With write access to this repository, save the description with the table to `body.md` and run `gh pr edit <PR> --body-file body.md --attach before.png --attach after.png`. gh uploads both files and points the references at them.
   - If neither route is available to you, ask the PR author to drag the images in.
5. **Done** when the rendered description shows both images and the words Before and After. Editing the description reruns the check.

A maintainer adds the `no-visual-change` label when the rendered page does not change, such as for an edit inside an HTML comment.
