# capital-etf-00992a-portfolio

自動抓取 00992A 群益台灣科技創新主動式 ETF 官方投資組合資料，並輸出標準化持股 CSV 與每日差異檔。

## 資料來源

- 官方頁面：https://www.capitalfund.com.tw/etf/product/detail/500/portfolio

## 輸出結構

```text
data/
├── raw/
│   └── 00992A_portfolio_YYYYMMDD.html
└── out/
    ├── 00992A_holdings_YYYYMMDD.csv
    ├── 00992A_latest.csv
    ├── 00992A_diff_YYYYMMDD.csv
    └── 00992A_diff_YYYYMMDD.md
```

## 標準化持股欄位

```text
code,name,weight,shares
```

- `code`：股票代號
- `name`：股票名稱
- `weight`：持股權重，單位為 `%`
- `shares`：持股股數

## GitHub Actions

預設排程：週一至週五台灣時間 16:40 自動執行。

也可以到 GitHub Actions 手動執行 `Download 00992A portfolio` workflow。
