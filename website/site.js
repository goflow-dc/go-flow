// Fills in download links, the version line, the contact email and the year from config.js.
(function () {
  var c = window.GOFLOW || {};
  var ready = c.githubUser && c.githubUser.indexOf("YOUR-") !== 0;
  var repoUrl = "https://github.com/" + c.githubUser + "/" + c.repo;
  var download = repoUrl + "/releases/latest/download/" + c.installer;

  function each(sel, fn) { Array.prototype.forEach.call(document.querySelectorAll(sel), fn); }

  each("[data-download]", function (a) { a.href = ready ? download : "support.html#download"; });
  each("[data-checksum]", function (a) { a.href = ready ? download + ".sha256" : "support.html#download"; });
  each("[data-releases]", function (a) { a.href = ready ? repoUrl + "/releases" : "changelog.html"; });
  each("[data-source]", function (a) { if (ready) a.href = repoUrl; else a.parentNode.removeChild(a); });
  each("[data-owner]", function (e) { e.textContent = c.owner || "Go Flow"; });
  each("[data-year]", function (e) { e.textContent = new Date().getFullYear(); });
  each("[data-email]", function (e) {
    if (c.email) { e.innerHTML = '<a href="mailto:' + c.email + '">' + c.email + "</a>"; }
    else if (e.hasAttribute("data-hide-empty")) { e.style.display = "none"; }
    else { e.textContent = ready ? "the Issues page of the project on GitHub" : "the contact address on this page once it is added"; }
  });

  // Version and size of the newest release, straight from GitHub (no tracking, no cookies).
  var line = document.querySelector("[data-version]");
  if (!line) return;
  if (!ready) { line.textContent = "Windows 10 / 11 · 64-bit · Free"; return; }
  fetch("https://api.github.com/repos/" + c.githubUser + "/" + c.repo + "/releases/latest")
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (rel) {
      if (!rel) return;
      var asset = (rel.assets || []).filter(function (a) { return a.name === c.installer; })[0];
      var parts = [rel.tag_name];
      if (asset) parts.push(Math.round(asset.size / 1048576) + " MB");
      parts.push("Windows 10 / 11");
      line.textContent = parts.join(" · ");
    })
    .catch(function () {});
})();
