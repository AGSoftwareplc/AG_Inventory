// Point of sale screen. The server re-checks prices, stock and totals; this script only helps the cashier.
(function () {
  var cart = [];          // {id, name, unit, price (cents), def (default price), stock, qty, sn (serial-numbered), serials}
  var results = [];
  var selIdx = 0;
  var $ = function (id) { return document.getElementById(id); };
  var search = $('search'), box = $('results'), timer = null;

  function money(c) {
    return (c / 100).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  }
  function qty(q) { return (+q || 0).toLocaleString('en-US', {maximumFractionDigits: 3}); }
  function num(v) { v = parseFloat(String(v || '').replace(/,/g, '')); return isNaN(v) ? 0 : v; }
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

  function find(q, cb) {
    fetch(POS.search + '?q=' + encodeURIComponent(q), {credentials: 'same-origin'})
      .then(function (r) { return r.json(); }).then(cb)
      .catch(function () { box.hidden = false; box.innerHTML = '<div>Search failed. Check the connection.</div>'; });
  }

  function showResults(list) {
    results = list; selIdx = 0;
    if (!list.length) { box.hidden = false; box.innerHTML = '<div class="muted">No product found.</div>'; return; }
    box.innerHTML = list.map(function (p, i) {
      return '<div data-i="' + i + '" class="' + (i === 0 ? 'sel' : '') + '"><span>' + esc(p.name) +
        ' <span class="muted">' + esc(p.code) + '</span></span><span>' + money(p.price) +
        ' · <span class="' + (p.stock <= 0 ? 'badge err' : 'muted') + '">' + qty(p.stock) + ' ' + esc(p.unit) + '</span></span></div>';
    }).join('');
    box.hidden = false;
  }

  function add(p) {
    var line = cart.find(function (l) { return l.id === p.id; });
    var sn = !!p.serial_tracked;
    if (!line) {
      line = {id: p.id, name: p.name, unit: p.unit, price: p.price, def: p.price, stock: p.stock, qty: sn ? 0 : 1,
              sn: sn, serials: []};
      cart.push(line);
      if (sn) loadSerials(line);
    } else if (!sn) line.qty = Math.round((line.qty + 1) * 1000) / 1000;
    if (sn && p.serial) addSerial(line, p.serial);
    search.value = ''; box.hidden = true; results = [];
    render();
    var si = sn && !p.serial && document.querySelector('[data-sn="' + cart.indexOf(line) + '"]');
    if (si) si.focus(); else search.focus();
  }

  // serial-numbered products: one serial number per item; the quantity is the number of serials
  function loadSerials(line) {
    fetch(POS.serials + '?pid=' + line.id, {credentials: 'same-origin'}).then(function (r) { return r.json(); })
      .then(function (list) { line.avail = list; render(); });
  }
  function addSerial(line, v) {
    v = String(v || '').trim();
    if (!v) return;
    if (line.serials.some(function (x) { return x.toLowerCase() === v.toLowerCase(); })) return;
    line.serials.push(v); line.qty = line.serials.length;
  }

  function render() {
    var body = document.querySelector('#cart tbody');
    body.innerHTML = cart.map(function (l, i) {
      var over = l.qty > l.stock;
      var name = esc(l.name) + (over ? ' <span class="badge err">only ' + qty(l.stock) + ' in stock</span>' : '');
      if (l.sn) {
        name += '<span class="ser">' + l.serials.map(function (x, k) {
          return '<span class="badge">' + esc(x) + ' <a href="#" data-unsn="' + i + ':' + k + '" title="Remove">×</a></span> ';
        }).join('') + '<input data-sn="' + i + '" list="snl' + i + '" placeholder="Serial no.: scan or type, Enter" autocomplete="off">' +
          '<datalist id="snl' + i + '">' + (l.avail || []).filter(function (x) { return l.serials.indexOf(x) < 0; })
            .map(function (x) { return '<option value="' + esc(x) + '">'; }).join('') + '</datalist></span>';
      }
      var price = POS.price ? '<input class="price" data-p="' + i + '" value="' + money(l.price) + '" inputmode="decimal">' +
        (l.price !== l.def ? '<div class="' + (l.price > l.def ? 'vpos' : 'vneg') + '" style="font-size:.8rem">' +
          (l.price > l.def ? '+' : '−') + money(Math.abs(l.price - l.def)) + ' vs ' + money(l.def) + '</div>' : '') : money(l.price);
      var q = l.sn ? '<b>' + l.qty + '</b> ' + esc(l.unit) :
        '<input data-i="' + i + '" value="' + l.qty + '" inputmode="decimal"> ' + esc(l.unit);
      return '<tr' + (over ? ' class="low"' : '') + '><td>' + name + '</td><td class="n">' + price + '</td><td class="n">' + q +
        '</td><td class="n">' + money(Math.round(l.qty * l.price)) +
        '</td><td><button type="button" class="x" data-del="' + i + '" title="Remove">×</button></td></tr>';
    }).join('');
    $('empty').hidden = cart.length > 0;
    totals();
  }

  function totals() {
    var sub = cart.reduce(function (s, l) { return s + Math.round(l.qty * l.price); }, 0);
    var disc = 0, d = $('disc');
    if (d && d.value) {
      disc = $('dtype').value === 'pct' ? Math.round(sub * num(d.value) / 100) : Math.round(num(d.value) * 100);
    }
    disc = Math.min(Math.max(disc, 0), sub);
    var grand = sub - disc;
    var tend = $('tendered').value === '' ? grand : Math.round(num($('tendered').value) * 100);
    $('sub').textContent = money(sub);
    $('discv').textContent = money(disc);
    $('grand').textContent = money(grand);
    $('change').textContent = money(Math.max(0, tend - grand));
    $('duebox').hidden = !(tend < grand);
    $('due').textContent = money(grand - tend);
    $('items').value = JSON.stringify(cart.map(function (l) {
      return {id: l.id, qty: l.qty, price: l.price === l.def ? null : l.price, serials: l.serials};
    }));
    return grand;
  }

  search.addEventListener('input', function () {
    clearTimeout(timer);
    var q = search.value.trim();
    if (!q) { box.hidden = true; return; }
    timer = setTimeout(function () { find(q, function (r) { showResults(r.items || []); }); }, 200);
  });
  search.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!results.length) return;
      e.preventDefault();
      selIdx = (selIdx + (e.key === 'ArrowDown' ? 1 : results.length - 1)) % results.length;
      box.querySelectorAll('[data-i]').forEach(function (el, i) { el.classList.toggle('sel', i === selIdx); });
    } else if (e.key === 'Enter') {
      e.preventDefault();
      var q = search.value.trim();
      if (!q) return;
      clearTimeout(timer);
      // Barcode scanners type fast and press Enter: always ask the server for an exact match
      find(q, function (r) {
        var items = r.items || [];
        if (r.exact && items.length === 1) add(items[0]);
        else if (items.length && results.length && results[selIdx] && search.value.trim() === q) add(results[selIdx]);
        else if (items.length === 1) add(items[0]);
        else showResults(items);
      });
    } else if (e.key === 'Escape') { box.hidden = true; }
  });
  box.addEventListener('click', function (e) {
    var el = e.target.closest('[data-i]');
    if (el && results[+el.dataset.i]) add(results[+el.dataset.i]);
  });
  document.querySelector('#cart tbody').addEventListener('keydown', function (e) {
    var i = e.target.dataset.sn;
    if (i !== undefined && e.key === 'Enter') {
      e.preventDefault();
      addSerial(cart[i], e.target.value); render();
      var si = document.querySelector('[data-sn="' + i + '"]'); if (si) si.focus();
    }
  });
  document.querySelector('#cart tbody').addEventListener('input', function (e) {
    var i = e.target.dataset.i, pi = e.target.dataset.p;
    if (pi !== undefined) {
      cart[pi].price = Math.max(0, Math.round(num(e.target.value) * 100));
      e.target.closest('tr').cells[3].textContent = money(Math.round(cart[pi].qty * cart[pi].price));
      totals();
      return;
    }
    if (i !== undefined) {
      cart[i].qty = Math.max(0, num(e.target.value));
      var tr = e.target.closest('tr');
      tr.cells[3].textContent = money(Math.round(cart[i].qty * cart[i].price));
      tr.classList.toggle('low', cart[i].qty > cart[i].stock);
      totals();
    }
  });
  document.querySelector('#cart tbody').addEventListener('change', function (e) {
    var i = e.target.dataset.sn;
    if (i !== undefined) {  // a serial picked from the list
      if (e.target.value && (cart[i].avail || []).indexOf(e.target.value) >= 0) addSerial(cart[i], e.target.value);
      else return;
    }
    render();
  });
  document.querySelector('#cart tbody').addEventListener('click', function (e) {
    if (e.target.dataset.del !== undefined) { cart.splice(+e.target.dataset.del, 1); render(); }
    if (e.target.dataset.unsn !== undefined) {
      e.preventDefault();
      var a = e.target.dataset.unsn.split(':'), l = cart[+a[0]];
      l.serials.splice(+a[1], 1); l.qty = l.serials.length; render();
    }
  });
  ['disc', 'dtype', 'tendered'].forEach(function (id) {
    if ($(id)) { $(id).addEventListener('input', totals); $(id).addEventListener('change', totals); }
  });
  $('payform').addEventListener('submit', function (e) {
    var noSn = cart.find(function (l) { return l.sn && !l.serials.length; });
    if (noSn) { e.preventDefault(); alert(noSn.name + ' needs a serial number for every item sold.'); return; }
    cart = cart.filter(function (l) { return l.qty > 0; });
    if (!cart.length) { e.preventDefault(); alert('The cart is empty.'); return; }
    var grand = totals();
    if ($('tendered').value === '') $('tendered').value = money(grand);
    if (Math.round(num($('tendered').value) * 100) < grand && !$('credit_ok').value) {
      e.preventDefault();
      alert('You are not allowed to sell on credit. The full amount must be paid.');
      return;
    }
    if (Math.round(num($('tendered').value) * 100) < grand && !$('cust').value) {
      e.preventDefault();
      alert('Amount paid is less than the total. Choose a registered customer to sell on credit.');
      return;
    }
    $('paybtn').disabled = true;
    $('paybtn').textContent = 'Saving…';
  });
  render();
})();
