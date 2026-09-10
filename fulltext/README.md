# 全文 PDF 下载

各数据库全文下载脚本和流程。题录采集在仓库根目录 `harvest/`。

```bat
copy .env.example .env
pip install -r ../requirements.txt
```

`.env` 里按通道填账号（浙大 / scihuber / 混沌 / 知献 / 无忧）。不要提交 `.env`。

公开 PDF：

```bat
python generic/download_all.py --out %BOOKNOTE_PDFS% --only-feasible
```

机构订阅（CARSI，CSV 列 `id,pdf_url`）：

```bat
python generic/real_crawl.py --map data\map.csv --limit 5
```

分库（先 session 再 bulk），流程见 [FLOWS.md](FLOWS.md)。
