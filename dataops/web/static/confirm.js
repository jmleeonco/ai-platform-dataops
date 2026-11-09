// Forms with data-confirm ask before they submit (the CSP allows no inline handlers).
document.addEventListener("submit", (e) => {
  const f = e.target.closest("form[data-confirm]");
  if (f && !window.confirm(f.dataset.confirm)) e.preventDefault();
});
