// ================= 0. 小工具 =================
// 备注里的链接（如「我的」栏目里的 /admin）加个小箭头，提示它是可以点的
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.item-note a').forEach(a => {
        a.classList.add('inline-link');
    });
});


// ================= 0.1 图标精灵工具 =================
// 图标全部来自本地 static/icons.svg（SVG <symbol> 精灵），用法：
//     <svg class="ic"><use href="{SPRITE}#i-bell"></use></svg>
// 这里集中管理精灵地址，JS 动态插入图标时不必各处硬编码路径。
const ICON_SPRITE = (() => {
    // 优先从页面已有的 <use> 上取，拿到的是模板里 url_for 生成的真实地址，
    // 因此即使以后给静态资源加了 CDN 前缀或改了挂载点，这里也会自动跟随。
    const existing = document.querySelector('use');
    const href = existing && (existing.getAttribute('href') || existing.getAttribute('xlink:href'));
    if (href && href.indexOf('#') > -1) return href.slice(0, href.indexOf('#'));
    return '/static/icons.svg';
})();

function iconSvg(name, className) {
    const cls = className || 'ic';
    return `<svg class="${cls}" aria-hidden="true"><use href="${ICON_SPRITE}#${name}"></use></svg>`;
}

// ================= 1. 底部导航栏切换与滚动动画 =================
const navItems = document.querySelectorAll('.nav-item');
const pages = document.querySelectorAll('.page');
const pageTitle = document.getElementById('pageTitle');
const bottomNav = document.getElementById('bottomNav');
const header = document.getElementById('mainHeader');

// 导航栏点击切换页面
navItems.forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        // 切换Active状态
        navItems.forEach(nav => nav.classList.remove('active'));
        item.classList.add('active');
        
        // 切换页面视图
        const targetId = item.getAttribute('data-target');
        const targetPage = document.getElementById(targetId);
        if (!targetPage) return; // 目标页不存在时不动作，避免整个脚本报错
        pages.forEach(page => page.classList.remove('active'));
        targetPage.classList.add('active');
        
        // 更新页眉标题：标题文字 + 标题前的图标一起换（图标名写在 data-icon 上）
        if (pageTitle) {
            pageTitle.textContent = item.getAttribute('data-title') || '';
            const iconName = item.getAttribute('data-icon');
            if (iconName) {
                const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                icon.setAttribute('class', 'ic');
                icon.setAttribute('aria-hidden', 'true');
                const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
                use.setAttribute('href', ICON_SPRITE + '#' + iconName);
                icon.appendChild(use);
                pageTitle.prepend(icon);
            }
        }
        
        // 滚动到顶部
        window.scrollTo({ top: 0, behavior: 'smooth' });
    });
});

// 页面滚动监听：控制导航栏和页眉阴影
window.addEventListener('scroll', () => {
    const currentScrollY = window.scrollY;

    // 页眉阴影效果：滚过一点就浮起，形成层次
    if (header) header.classList.toggle('scrolled', currentScrollY > 10);
});



// ================= 附件功能 =================
document.addEventListener('change', (e) => {
    if (!e.target.classList.contains('attachment-input')) return;
    
    const files = Array.from(e.target.files);
    if (files.length === 0) return;
    
    const listContainer = e.target.closest('.attachment-block').querySelector('.attachment-list');
    
    files.forEach(file => {
        const item = document.createElement('div');
        item.className = 'attachment-item';
        
        // 根据文件类型生成预览
        if (file.type.startsWith('image/')) {
            const img = document.createElement('img');
            img.src = URL.createObjectURL(file);
            img.onload = () => URL.revokeObjectURL(img.src); // 释放内存
            item.appendChild(img);
        } else {
            const iconDiv = document.createElement('div');
            iconDiv.className = 'file-icon';
            const ext = file.name.split('.').pop().toUpperCase();
            iconDiv.innerHTML = `${iconSvg('i-file')}<span>${ext}</span>`;
            item.appendChild(iconDiv);
        }
        
        // 删除按钮
        const removeBtn = document.createElement('button');
        removeBtn.className = 'attachment-remove';
        removeBtn.setAttribute('aria-label', '移除');
        removeBtn.innerHTML = iconSvg('i-close');
        removeBtn.addEventListener('click', () => {
            item.style.animation = 'fadeIn 0.2s ease reverse';
            setTimeout(() => item.remove(), 200);
        });
        item.appendChild(removeBtn);
        
        listContainer.appendChild(item);
    });
    
    // 重置 input，允许重复选择同一文件
    e.target.value = '';
});


// ------------ 附件上传功能 --------------

const UploadManager = {
    // ⚠️ 配置区：修改为你的真实后端地址
    API_URL: '/api/upload', 
    MAX_SIZE: 10 * 1024 * 1024, // 10MB
    
    // 初始化事件委托
    init() {
        document.addEventListener('change', (e) => {
            if (!e.target.classList.contains('attachment-input')) return;
            const files = Array.from(e.target.files);
            const block = e.target.closest('.attachment-block');
            const itemId = block.dataset.itemId;
            const list = block.querySelector('.attachment-list');
            
            files.forEach(file => this.handleFile(file, list, itemId));
            e.target.value = ''; // 重置以允许重复选择
        });
        
        // 重试按钮点击
        document.addEventListener('click', (e) => {
            if (!e.target.classList.contains('retry-btn')) return;
            const item = e.target.closest('.attachment-item');
            const file = item._fileRef;
            const itemId = item.closest('.attachment-block').dataset.itemId;
            this.resetItem(item);
            this.upload(file, item, itemId);
        });
    },
    
    // 文件预处理与校验
    handleFile(file, listContainer, itemId) {
        if (file.size > this.MAX_SIZE) {
            alert(`文件 "${file.name}" 超过10MB限制`);
            return;
        }
        
        const itemEl = this.createItemElement(file);
        listContainer.appendChild(itemEl);
        itemEl._fileRef = file; // 保存引用供重试用
        
        this.upload(file, itemEl, itemId);
    },
    
    // 创建带进度条的DOM元素
    createItemElement(file) {
        const div = document.createElement('div');
        div.className = 'attachment-item uploading';
        
        // 预览内容
        if (file.type.startsWith('image/')) {
            const img = document.createElement('img');
            img.src = URL.createObjectURL(file);
            img.onload = () => URL.revokeObjectURL(img.src);
            div.appendChild(img);
        } else {
            const ext = file.name.split('.').pop().toUpperCase();
            div.innerHTML = `<div class="file-icon">${iconSvg('i-file')}<span>${ext}</span></div>`;
        }
        
        // 进度遮罩 + SVG环形进度
        const circumference = 2 * Math.PI * 11; // r=11
        div.innerHTML += `
            <div class="upload-overlay">
                <svg class="progress-ring" viewBox="0 0 28 28">
                    <circle class="progress-ring__circle" cx="14" cy="14" r="11" 
                        stroke-dasharray="${circumference}" 
                        stroke-dashoffset="${circumference}"/>
                </svg>
                <span style="color:#fff;font-size:9.5px;" class="progress-text">0%</span>
            </div>
            <div class="upload-status-icon success">${iconSvg('i-check')}</div>
            <div class="upload-status-icon error">${iconSvg('i-info')}</div>
            <button class="retry-btn">重试</button>
            <button class="attachment-remove" style="display:none;">${iconSvg('i-close')}</button>
        `;
        
        // 删除按钮（上传完成后显示）
        div.querySelector('.attachment-remove').addEventListener('click', () => {
            div.style.animation = 'fadeIn 0.2s ease reverse';
            setTimeout(() => div.remove(), 200);
        });
        
        return div;
    },
    
    resetItem(itemEl) {
        itemEl.classList.remove('failed', 'success');
        itemEl.classList.add('uploading');
        itemEl.querySelector('.upload-overlay').classList.remove('hidden');
        itemEl.querySelector('.upload-status-icon.success').classList.remove('success');
        itemEl.querySelector('.upload-status-icon.error').classList.remove('error');
        itemEl.querySelector('.attachment-remove').style.display = 'none';
    },
    
    // 🚀 核心上传方法
    async upload(file, itemEl, itemId) {
        const circle = itemEl.querySelector('.progress-ring__circle');
        const progressText = itemEl.querySelector('.progress-text');
        const overlay = itemEl.querySelector('.upload-overlay');
        const circumference = 2 * Math.PI * 11;
        
        try {
            const formData = new FormData();
            formData.append('file', file);
            formData.append('itemId', itemId);
            
            // ✅ 真实上传（取消注释下方代码，删除模拟代码）
            /*
            const response = await fetch(this.API_URL, {
                method: 'POST',
                body: formData,
                // 注意：不要手动设置 Content-Type，浏览器会自动添加 boundary
            });
            if (!response.ok) throw new Error(`服务器错误: ${response.status}`);
            const result = await response.json();
            console.log('上传成功:', result);
            */
            
            // 🔄 模拟上传进度（演示用，实际使用时删除此段）
            await new Promise((resolve, reject) => {
                let progress = 0;
                const timer = setInterval(() => {
                    progress += Math.random() * 15;
                    if (progress >= 100) {
                        progress = 100;
                        clearInterval(timer);
                        // 模拟90%概率成功
                        Math.random() > 0.1 ? resolve() : reject(new Error('网络超时'));
                    }
                    const offset = circumference - (progress / 100) * circumference;
                    circle.style.strokeDashoffset = offset;
                    progressText.textContent = `${Math.round(progress)}%`;
                }, 200);
            });
            
            // ✅ 上传成功
            itemEl.classList.remove('uploading');
            itemEl.classList.add('success');
            overlay.classList.add('hidden');
            itemEl.querySelector('.upload-status-icon.success').classList.add('success');
            itemEl.querySelector('.attachment-remove').style.display = 'flex';
            
        } catch (err) {
            // ❌ 上传失败
            itemEl.classList.remove('uploading');
            itemEl.classList.add('failed');
            overlay.classList.add('hidden');
            itemEl.querySelector('.upload-status-icon.error').classList.add('error');
            console.error('上传失败:', err);
        }
    }
};

// 初始化上传模块
UploadManager.init();


// ================= 已完成事项折叠区（懒加载） =================
// 折叠条由模板渲染，跟随栏目内容排在列表末尾；内容默认不存在于 DOM 中，
// 只有用户点击展开时才请求 /api/notices?folded=1，未展开则完全不查库。
const FoldManager = {
    // 各分类的默认标签样式，需与 index.html 宏里的 tag_class 保持一致。
    // notice 故意用中性灰 tag-notice：蓝色留给「级别徽标」，避免整行都是蓝的。
    TAG_CLASS: { notice: 'tag-notice', homework: 'tag-homework', online: 'tag-online' },

    init() {
        document.querySelectorAll('.fold-bar').forEach(bar => {
            bar.addEventListener('click', () => this.toggle(bar));
        });
    },

    esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
            ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    },

    bodyOf(bar) {
        const cat = bar.dataset.cat;
        return bar.parentElement.querySelector(`.fold-body[data-cat="${cat}"]`);
    },

    async toggle(bar) {
        const body = this.bodyOf(bar);
        if (!body) return;

        const open = !bar.classList.contains('open');
        bar.classList.toggle('open', open);
        body.classList.toggle('open', open);
        if (!open) return;                       // 收起时不请求

        if (body.dataset.loaded === '1') return;  // 已加载过就不重复请求

        body.innerHTML = '<div class="fold-loading">加载中…</div>';
        try {
            const res = await fetch('/api/notices?category=' + encodeURIComponent(bar.dataset.cat) + '&folded=1');
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const json = await res.json();
            const rows = json.data || [];
            body.innerHTML = rows.length
                ? rows.map(n => this.renderItem(n, bar.dataset.cat)).join('')
                : '<div class="fold-loading">没有更早的已完成事项。</div>';
            body.dataset.loaded = '1';
            // 新插入的卡片无需额外注册：CountdownManager 每秒全局扫描
            // .list-item[data-deadline]，下一次 tick 就会自动接管它们的倒计时
        } catch (err) {
            // 失败时不置 loaded 标记，再次点击可重试
            body.innerHTML = '<div class="fold-loading">加载失败，点击重试。</div>';
            console.error('折叠区加载失败:', err);
        }
    },

    // 与 index.html 的 item_card 宏结构保持一致，含 link 包裹
    renderItem(n, cat) {
        // 级别类名与模板宏同源：普通也有自己的类（v1.7.0 起换雾蓝配色），
        // 否则从折叠区渲染出来的普通卡片会和首页的普通卡片长得不一样
        const cls = n.pinned ? ' pinned'
            : (n.level === 'urgent' ? ' urgent'
                : (n.level === 'done' ? ' done' : ' normal'));

        const levelName = (window.CLASS_CONFIG.levelName || {})[n.level] || '';
        const tagClass = n.pinned ? 'tag-pinned'
            : (n.level === 'urgent' ? 'tag-urgent'
                : (n.level === 'done' ? 'tag-done'
                    : (this.TAG_CLASS[cat] || 'tag-normal')));

        // 已接龙时级别徽标写「已接龙」，与模板宏保持一致
        const levelText = n.joined ? '已接龙' : levelName;
        const rollTags = n.rollcall
            ? `<span class="item-tag tag-roll">${iconSvg('i-users')}${this.esc(n.rollcount || 0)} 人接龙</span>`
            : '';

        // data-* 与模板保持一致，客户端倒计时刷新依赖它们
        // 结构也与 index.html 的 item_card 宏一一对应：小信息块统一装进 .item-tags
        // 排成一行；只有未接龙的接龙卡片才显示 .roll-cta（已接龙的那句提示已删）
        // v1.9.0：截止时间已过的接龙用中性的 .closed 提示（卡片仍可点进去看名单）。
        // 判 `=== false` 而不是取反：字段缺失（老接口）时退回"可点"的老文案，
        // 别把还能报名的卡片误标成已截止。
        const rollCta = n.roll_open === false
            ? `<div class="roll-cta closed">${iconSvg('i-users')}接龙已截止，点击查看名单</div>`
            : `<div class="roll-cta">${iconSvg('i-users')}点击卡片进入接龙</div>`;
        const card = `
        <div class="list-item${cls}" data-deadline="${this.esc(n.deadline || '')}"
             data-category="${this.esc(n.category || cat)}" data-pinned="${n.pinned ? 1 : 0}"
             data-joined="${n.joined ? 1 : 0}">
            <div class="item-content">
                <span class="item-name">${n.pinned ? iconSvg('i-pin', 'ic pin-icon') : ''}${this.esc(n.title)}</span>
                <div class="item-tags">
                    <span class="item-tag ${tagClass}">${this.esc(levelText)}</span>
                    ${rollTags}
                    ${n.tag ? `<span class="item-tag ${this.TAG_CLASS[cat] || 'tag-normal'}">${this.esc(n.tag)}</span>` : ''}
                    ${n.countdown ? `<span class="item-countdown">${this.esc(n.countdown)}</span>` : ''}
                </div>
                ${n.note ? `<div class="item-note">${this.esc(n.note)}</div>` : ''}
                ${n.rollcall && !n.joined ? rollCta : ''}
            </div>
            <div class="item-meta">
                <div>
                    ${n.date_label ? `<div class="item-time">${this.esc(n.date_label)}</div>` : ''}
                    ${n.time_label ? `<div class="item-time-sub">${this.esc(n.time_label)}</div>` : ''}
                </div>
                ${n.location ? `<div class="item-location">${iconSvg('i-pin-loc')}${this.esc(n.location)}</div>` : ''}
            </div>
        </div>`;

        // 接龙事项整块可点，进入接龙页；接龙优先于外链（外链在接龙页里还有一个按钮）。
        // 其余链接需转义后作为 href，避免 javascript: 之类的注入。
        if (n.rollcall) {
            return `<a href="/rollcall/${encodeURIComponent(n.id)}">${card}</a>`;
        }
        if (n.link && /^https?:\/\//i.test(n.link)) {
            return `<a href="${this.esc(n.link)}">${card}</a>`;
        }
        return card;
    }
};

FoldManager.init();


// ================= 客户端倒计时自动刷新 =================
// 页面加载完成后，剩余时间由浏览器本地时钟每秒重算，不必反复请求服务器。
// 剩余不足 SECOND_PRECISION_MINUTES（默认 10）分钟时精确到秒。
// 级别（紧急/普通/已完成）也随之在本地切换，无需刷新页面。
const CountdownManager = {
    cfg() {
        const c = window.CLASS_CONFIG || {};
        return {
            urgentHours: c.urgentHours || {},
            levelName: c.levelName || { pinned: '置顶', urgent: '紧急', normal: '普通', done: '已完成' },
            secondMinutes: c.secondPrecisionMinutes || 10,
        };
    },

    // 与后端 db.compute_level 保持一致
    levelOf(category, deadline) {
        if (!deadline) return 'normal';
        const ms = deadline - Date.now();
        if (ms <= 0) return 'done';
        const threshold = (this.cfg().urgentHours[category] || 24) * 3600 * 1000;
        return ms <= threshold ? 'urgent' : 'normal';
    },

    // 与后端 db._duration_text 一致：≥10 分钟只到分，<10 分钟到秒
    durationText(ms) {
        let total = Math.floor(Math.abs(ms) / 1000);
        if (total < 0) total = 0;
        const d = Math.floor(total / 86400); total %= 86400;
        const h = Math.floor(total / 3600); total %= 3600;
        const m = Math.floor(total / 60);
        const s = total % 60;
        if (d) return d + '天' + h + '小时';
        if (h) return h + '小时' + m + '分';
        if (m >= this.cfg().secondMinutes) return m + '分钟';
        if (m) return m + '分' + s + '秒';
        return s + '秒';
    },

    // 服务端渲染的是 "YYYY-MM-DD HH:MM"，Safari 不认空格分隔的写法，需转成 ISO
    parse(raw) {
        if (!raw) return null;
        const t = Date.parse(String(raw).trim().replace(' ', 'T'));
        return Number.isNaN(t) ? null : t;
    },

    applyCard(card) {
        const raw = card.dataset.deadline;
        const ts = this.parse(raw);
        const cd = card.querySelector('.item-countdown');
        // 已接龙 = 这件事对我已经办完：级别恒为已完成，也不该再倒数
        // （服务端已经不再输出倒计时文案，这里必须同样跳过，否则会被每秒覆盖）
        if (card.dataset.joined === '1') {
            if (cd) cd.textContent = '';
            return;
        }
        // 没有截止时间就没有倒计时，也无需重算级别
        if (ts === null) {
            if (cd) cd.textContent = '';
            return;
        }

        const category = card.dataset.category || 'notice';
        const pinned = card.dataset.pinned === '1';
        // 置顶优先于一切，级别恒为 pinned，不随时间降级
        const level = pinned ? 'pinned' : this.levelOf(category, ts);
        const ms = ts - Date.now();
        const text = (ms <= 0 ? '已过 ' : '剩 ') + this.durationText(ms);

        if (cd) cd.textContent = text;

        // 级别变化时同步更新卡片配色与级别徽标（置顶项不参与）
        if (!pinned && card.dataset.level !== level) {
            card.dataset.level = level;
            // 三个状态类都要清掉，再挂当前这个：漏掉 normal 的话，
            // 一条「普通」事项到点变「紧急」后会同时带着雾蓝底和粉红书脊
            card.classList.remove('urgent', 'done', 'normal');
            if (level === 'urgent') card.classList.add('urgent');
            else if (level === 'done') card.classList.add('done');
            else card.classList.add('normal');
            const tag = card.querySelector('.item-tag');
            if (tag && !tag.classList.contains('tag-pinned')) {
                tag.className = 'item-tag tag-' + level;
                tag.textContent = this.cfg().levelName[level] || '';
            }
        }
    },

    tick() {
        document.querySelectorAll('.list-item[data-deadline]').forEach(c => this.applyCard(c));
    },

    init() {
        // 给服务端渲染出的卡片打上初始级别标记，后续只在变化时更新
        document.querySelectorAll('.list-item[data-deadline]').forEach(c => {
            const ts = this.parse(c.dataset.deadline);
            if (ts === null) return;
            if (c.dataset.pinned === '1') return;
            c.dataset.level = this.levelOf(c.dataset.category || 'notice', ts);
        });
        this.tick();
        setInterval(() => this.tick(), 1000);
    }
};

CountdownManager.init();