# VC-R-01 订单与退货管理系统

基于订单明细的退货额度查询与退货申请校验示例。系统可导入清洗后的 CSV，计算可退数量，拦截重复及超量申请，并记录申请状态与流转历史。项目使用 SQLite 保存本地演示状态。

## 功能

- 导入 CSV 并校验数据基线和不变量
- 查询发票明细、历史退货、占用数量和剩余可退数量
- 创建、受理、收货、结案或拒绝退货申请
- 对重复、超量、非法状态转换等情况返回稳定结果码
- 本地网页界面和命令行入口

## 环境与运行

Python 3.11 或更新版本。系统、测试和网页界面使用 Python 标准库；只有从 UCI 原始 XLSX 重建清洗 CSV 时需要 `openpyxl`。

```bash
python -m unittest discover -s tests -v
python src/main.py --help
```

数据准备方式和预期列见 [data/README.md](data/README.md)。没有数据文件时，先下载并准备数据，再导入：

```bash
python src/prepare.py --xlsx data/raw/online_retail_II.xlsx
python src/main.py import --csv data/build/order_line.csv --db data/build/return_system.db --out data/build
```

启动网页界面的入口、参数和命令见 `src/webui/`、`src/main.py` 与 `tools/`。网页界面仅绑定本机回环地址时适合本地演示；公开部署前需自行配置访问控制和网络边界。

## 数据与隐私

本仓库不附带 UCI 原始工作簿、从它生成的交易切片、数据库、报告或课程提交材料。请从 [UCI Online Retail II](https://archive.ics.uci.edu/dataset/502/online%2Bretail%2Bii) 获取数据，并按其 CC BY 4.0 条款注明来源。数据集引用：Chen, D. (2012). *Online Retail II*. UCI Machine Learning Repository. https://doi.org/10.24432/C5CG6D.

交易数据含客户编号等字段。处理真实业务数据前，应确认授权并采取适当的数据最小化与访问控制措施。

## 项目状态

这是一个课程项目/演示系统，不是可直接用于生产退货处理的服务。规则和数据口径以源码及随附说明为准。

## 许可

项目代码采用 MIT License，见 [LICENSE](LICENSE)。第三方数据仍受其各自许可约束。
