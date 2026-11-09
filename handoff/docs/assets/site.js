// Builds the site navigation at the top of every docs page and marks the current one.
// Pages are static; this is the only script. Paths are relative to the docs root (data-root on <body>).
(function () {
  var root = document.body.getAttribute('data-root') || './';
  var pages = [
    ['Overview', 'index.html', 'start'],
    ['Architecture', 'architecture.html', 'start'],
    ['Data flows', 'flows.html', 'start'],
    ['Roles', 'roles.html', 'start'],
    ['Hub', 'components/hub.html', 'components'],
    ['Executor', 'components/executor.html', 'components'],
    ['ai-pipeline', 'components/ai-pipeline.html', 'components'],
    ['OncoStudio', 'components/oncostudio.html', 'components'],
    ['Labeling', 'guides/labeling.html', 'guides'],
    ['Rounds', 'guides/rounds.html', 'guides'],
    ['Review', 'guides/review.html', 'guides'],
    ['Datasets & splits', 'guides/datasets.html', 'guides'],
    ['Training', 'guides/training.html', 'guides'],
    ['Deploy', 'guides/deploy.html', 'guides'],
    ['Releases', 'guides/releases.html', 'guides']
  ];
  var here = location.pathname.replace(/\/index\.html$/, '/').replace(/\/$/, '/index.html');
  var nav = document.createElement('nav');
  nav.className = 'site';
  var brand = document.createElement('a');
  brand.className = 'brand'; brand.href = root + 'index.html'; brand.textContent = 'OncoMLOps docs';
  nav.appendChild(brand);
  var lastGroup = '';
  pages.forEach(function (p) {
    if (p[2] !== lastGroup) {
      var g = document.createElement('span'); g.className = 'grp'; g.textContent = p[2]; nav.appendChild(g); lastGroup = p[2];
    }
    var a = document.createElement('a'); a.href = root + p[1]; a.textContent = p[0];
    if (here.endsWith('/' + p[1])) a.className = 'current';
    nav.appendChild(a);
  });
  var wrap = document.querySelector('.wrap') || document.body;
  wrap.insertBefore(nav, wrap.firstChild);
  // table of contents from h2 ids, when a .toc placeholder exists
  var toc = document.querySelector('.toc');
  if (toc) {
    document.querySelectorAll('h2[id]').forEach(function (h) {
      var a = document.createElement('a'); a.href = '#' + h.id; a.textContent = h.textContent; toc.appendChild(a);
    });
  }
})();
