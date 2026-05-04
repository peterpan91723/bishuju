const TABS_CONFIG = {
    yesterdayChange: { header: "涨幅 (%)", format: v => formatPercent(v.value) },
    weeklyClosedVolume: { header: "成交额 (USDT)", format: v => v.valueFormatted },
    monthlyClosedVolume: { header: "成交额 (USDT)", format: v => v.valueFormatted },
    fundingRate: { header: "费率 (%)", format: v => formatFunding(v.value) },
    weeklyRsi: { header: "成交额 (USDT)", format: v => v.valueFormatted, subFormat: v => `RSI: ${v.rsiPrev} → ${v.rsiCurr} ↑ 递增` },
    dailyRsi70: { header: "成交额 (USDT)", format: v => v.valueFormatted, subFormat: v => `RSI ${v.rsi} | EMA 9>21>55 间距扩张 | 量>MA20` },
    dailyRsi60: { header: "成交额 (USDT)", format: v => v.valueFormatted, subFormat: v => `RSI ${v.rsi} | EMA 9>21>55 间距扩张 | 量>MA20` },
};

let data = null;
let currentTab = "yesterdayChange";
let sortAsc = false; // false=降序, true=升序

function formatPercent(val) {
    const sign = val >= 0 ? "+" : "";
    return `${sign}${val.toFixed(2)}%`;
}

function formatFunding(val) {
    const sign = val >= 0 ? "+" : "";
    return `${sign}${val.toFixed(5)}%`;
}

function getColorClass(val, tab) {
    if (tab === "weeklyClosedVolume" || tab === "monthlyClosedVolume" || tab === "weeklyRsi" || tab === "dailyRsi70" || tab === "dailyRsi60") return "neutral";
    if (val > 0) return "positive";
    if (val < 0) return "negative";
    return "neutral";
}

function getRankClass(rank) {
    if (rank === 1) return "rank-top1";
    if (rank === 2) return "rank-top2";
    if (rank === 3) return "rank-top3";
    return "";
}

function stripUSDT(symbol) {
    return symbol.endsWith("USDT") ? symbol.slice(0, -4) : symbol;
}

function getSortedItems() {
    const items = [...(data[currentTab] || [])];
    if (sortAsc) {
        items.sort((a, b) => a.value - b.value);
    } else {
        items.sort((a, b) => b.value - a.value);
    }
    return items;
}

function renderTable() {
    if (!data) return;

    const config = TABS_CONFIG[currentTab];
    if (!config) return;

    const items = getSortedItems();
    const tbody = document.getElementById("rankBody");
    const header = document.getElementById("valueHeader");
    if (!tbody || !header) return;

    const arrow = sortAsc ? " ▲" : " ▼";
    header.innerHTML = config.header + `<span class="sort-arrow">${arrow}</span>`;

    if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" class="loading">暂无数据</td></tr>';
        return;
    }

    tbody.innerHTML = items
        .map((item, i) => {
            const rank = i + 1;
            const rankClass = getRankClass(rank);
            const colorClass = getColorClass(item.value, currentTab);
            const displayValue = config.format(item);
            const checked = selectedSymbols.has(item.symbol) ? "checked" : "";

            const subInfo = config.subFormat ? `<div class="sub-info">${config.subFormat(item)}</div>` : "";

            return `<tr>
                <td class="col-check"><input type="checkbox" class="symbol-check" data-symbol="${item.symbol}" ${checked}></td>
                <td class="col-rank ${rankClass}">${rank}</td>
                <td class="col-symbol">
                    <span class="symbol-name">${stripUSDT(item.symbol)} <span>/ USDT</span></span>${subInfo}
                </td>
                <td class="col-value ${colorClass}">${displayValue}</td>
            </tr>`;
        })
        .join("");

    updateExportBar();
}

function switchTab(tab) {
    currentTab = tab;
    sortAsc = (tab === "fundingRate"); // 资金费率默认升序，其他默认降序
    document.querySelectorAll(".tab").forEach(el => {
        el.classList.toggle("active", el.dataset.tab === tab);
    });
    renderTable();
}

function toggleSort() {
    sortAsc = !sortAsc;
    renderTable();
}

async function loadData() {
    try {
        const resp = await fetch("data/rankings.json?" + Date.now());
        data = await resp.json();
        document.getElementById("updateTime").textContent =
            `数据更新时间: ${data.updateTime}`;
        renderTable();
    } catch (e) {
        document.getElementById("updateTime").textContent = "数据加载失败，请先运行 fetch_data.py";
        document.getElementById("rankBody").innerHTML =
            '<tr><td colspan="3" class="loading">无法加载数据</td></tr>';
    }
}

// === 勾选与导出 ===
const selectedSymbols = new Set();

function updateExportBar() {
    const bar = document.getElementById("exportBar");
    const count = document.getElementById("selectedCount");
    const checkAll = document.getElementById("checkAll");
    if (selectedSymbols.size > 0) {
        bar.style.display = "flex";
        count.textContent = `已选 ${selectedSymbols.size} 个`;
    } else {
        bar.style.display = "none";
    }
    // 同步全选框状态
    const checks = document.querySelectorAll(".symbol-check");
    if (checks.length > 0) {
        checkAll.checked = [...checks].every(c => c.checked);
    }
}

function exportTradingViewTxt() {
    if (selectedSymbols.size === 0) return;
    const lines = [...selectedSymbols].map(s => `BINANCE:${s}.P`);
    const blob = new Blob([lines.join("\n")], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "tradingview_watchlist.txt";
    a.click();
    URL.revokeObjectURL(a.href);
}

// 表格内勾选事件（事件委托）
document.getElementById("rankBody").addEventListener("change", e => {
    if (e.target.classList.contains("symbol-check")) {
        const symbol = e.target.dataset.symbol;
        if (e.target.checked) {
            selectedSymbols.add(symbol);
        } else {
            selectedSymbols.delete(symbol);
        }
        updateExportBar();
    }
});

// 全选
document.getElementById("checkAll").addEventListener("change", e => {
    const checks = document.querySelectorAll(".symbol-check");
    checks.forEach(c => {
        c.checked = e.target.checked;
        if (e.target.checked) {
            selectedSymbols.add(c.dataset.symbol);
        } else {
            selectedSymbols.delete(c.dataset.symbol);
        }
    });
    updateExportBar();
});

// 全选按钮
document.getElementById("selectAllBtn").addEventListener("click", () => {
    const checks = document.querySelectorAll(".symbol-check");
    const allChecked = [...checks].every(c => c.checked);
    checks.forEach(c => {
        c.checked = !allChecked;
        if (!allChecked) {
            selectedSymbols.add(c.dataset.symbol);
        } else {
            selectedSymbols.delete(c.dataset.symbol);
        }
    });
    updateExportBar();
});

// 导出按钮
document.getElementById("exportBtn").addEventListener("click", exportTradingViewTxt);

// Tab click events
document.getElementById("tabs").addEventListener("click", e => {
    if (e.target.classList.contains("tab")) {
        switchTab(e.target.dataset.tab);
    }
});

// Sort toggle on header click
document.getElementById("valueHeader").addEventListener("click", toggleSort);

// Initial load
loadData();

// Auto refresh every 30s
setInterval(loadData, 30000);
