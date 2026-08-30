// The only JavaScript in the app: change a category inline and swap the row
// the server sends back. No framework, no CDN, works with the machine offline.
document.addEventListener('change', async (event) => {
  const select = event.target;
  if (!select.classList.contains('cat-select')) return;
  const category = select.value;
  if (!category) return;

  const teachBox = document.getElementById('teach');
  const body = new FormData();
  body.append('category', category);
  body.append('teach', teachBox && teachBox.checked ? 'true' : 'false');

  select.disabled = true;
  try {
    const response = await fetch(`/transactions/${select.dataset.tx}/category`, {
      method: 'POST', body,
    });
    if (!response.ok) throw new Error(await response.text());
    const row = document.getElementById(`tx-${select.dataset.tx}`);
    row.outerHTML = await response.text();
  } catch (err) {
    select.disabled = false;
    const row = document.getElementById(`tx-${select.dataset.tx}`);
    if (row) row.classList.add('save-failed');
    console.error('Could not save that category:', err);
  }
});
