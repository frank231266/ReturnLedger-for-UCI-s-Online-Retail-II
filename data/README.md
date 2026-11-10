# Dataset setup

The source dataset is UCI Machine Learning Repository's Online Retail II (DOI: 10.24432/C5CG6D), licensed CC BY 4.0. Download `online_retail_II.xlsx` from https://archive.ics.uci.edu/dataset/502/online%2Bretail%2Bii and place it at `data/raw/online_retail_II.xlsx`.

Install the optional parser dependency and generate the prepared CSV:

```bash
python -m pip install -r requirements.txt
python src/prepare.py --xlsx data/raw/online_retail_II.xlsx
```

The generated `data/build/` files are intentionally excluded from this repository. Do not commit customer-level source data or derived transaction extracts. Attribute the dataset to Chen (2012), UCI Machine Learning Repository, DOI 10.24432/C5CG6D, and follow CC BY 4.0.
