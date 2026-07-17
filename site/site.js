"use strict";

async function loadJson(path) {
  const response = await fetch(path, { cache: "no-cache" });
  if (!response.ok) {
    throw new Error(`Unable to load ${path}: ${response.status}`);
  }
  return response.json();
}

async function showArtifactMetadata() {
  const [sources, actors, schema] = await Promise.all([
    loadJson("artifacts/sources.json"),
    loadJson("artifacts/actors.json"),
    loadJson("artifacts/assessment.schema.json"),
  ]);

  const sourceCount = Array.isArray(sources.sources) ? sources.sources.length : null;
  const actorCount = Array.isArray(actors.actors) ? actors.actors.length : null;
  const schemaVersion = typeof schema.$schema === "string" ? schema.$schema : "";

  if (sourceCount !== null) {
    document.querySelector("#source-count").textContent = `${sourceCount} entries`;
  }
  if (actorCount !== null) {
    document.querySelector("#actor-count").textContent = `${actorCount} entries`;
  }
  if (schemaVersion.includes("2020-12")) {
    document.querySelector("#schema-version").textContent = "Draft 2020-12";
  }
}

showArtifactMetadata().catch(() => {
  // Direct artifact links remain useful when metadata enhancement is unavailable.
});
