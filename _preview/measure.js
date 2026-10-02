(function () {
    var out = [];
    function log(s) { out.push(s); }

    var de = document.documentElement;
    log('viewport innerWidth=' + window.innerWidth);
    log('documentElement scrollWidth=' + de.scrollWidth);
    log('body scrollWidth=' + document.body.scrollWidth);

    // 找出所有宽度超过视口的元素，按超出量排序
    var all = document.querySelectorAll('body *');
    var bad = [];
    for (var i = 0; i < all.length; i++) {
        var el = all[i];
        var r = el.getBoundingClientRect();
        if (r.width > window.innerWidth + 1 || r.right > window.innerWidth + 1) {
            bad.push({
                tag: el.tagName.toLowerCase(),
                cls: (el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className) || '',
                w: Math.round(r.width),
                left: Math.round(r.left),
                right: Math.round(r.right)
            });
        }
    }
    bad.sort(function (a, b) { return b.right - a.right; });
    log('--- overflow elements (' + bad.length + ') ---');
    bad.slice(0, 25).forEach(function (b) {
        log(b.tag + '.' + String(b.cls).slice(0, 40) + ' w=' + b.w + ' left=' + b.left + ' right=' + b.right);
    });

    // 关键元素的盒模型
    var probes = ['.list-item', '.item-content', '.item-meta', '.bottom-nav', '.page', '.header'];
    probes.forEach(function (sel) {
        var el = document.querySelector(sel);
        if (!el) { log('probe ' + sel + ' = none'); return; }
        var cs = getComputedStyle(el);
        var r = el.getBoundingClientRect();
        log('probe ' + sel + ' w=' + Math.round(r.width) + ' left=' + Math.round(r.left) +
            ' right=' + Math.round(r.right) + ' flexBasis=' + cs.flexBasis + ' minWidth=' + cs.minWidth +
            ' display=' + cs.display + ' padding=' + cs.paddingLeft + '/' + cs.paddingRight);
    });

    // 检查样式表是否真的生效
    var sheets = [];
    for (var s = 0; s < document.styleSheets.length; s++) {
        try { sheets.push((document.styleSheets[s].href || 'inline') + ' rules=' + document.styleSheets[s].cssRules.length); }
        catch (e) { sheets.push((document.styleSheets[s].href || '?') + ' ERR'); }
    }
    log('--- stylesheets ---');
    sheets.forEach(log);

    var pre = document.createElement('pre');
    pre.id = 'layout-dump';
    pre.textContent = out.join('\n');
    document.body.appendChild(pre);
})();
