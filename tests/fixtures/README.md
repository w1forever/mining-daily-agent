# tests/fixtures 说明

本目录中的夹具（明确标注）：
- ni43_101_sample.pdf    真实 Sigma Lithium Grota do Cirilo NI 43-101 技术报告
  （2023-01-16 发布，生效日 2022-10-31，568 页，17,766,706 字节）。
  下载自 https://sigmalithiumresources.com/wp-content/uploads/2023/05/2023-01-SGML-Updated-Technical-Report-1.pdf
  SHA256: 48845B0E0D4A88BE5608AB6195E56C32837484E39797086D454BDF72ED0CB25A
  用途：隔离外部网络依赖，保证 PDF 解析测试可复现。真实网络验证见
  tests/integration（RUN_LIVE_TESTS=1）。
- ni43_101_sample.url.txt 上述 PDF 的原始 URL。
- sample_news.html        构造的 HTML 正文样本（合成，仅测试 trafilatura 清洗）。

其他所有 Mock/Fake 数据均为代码内明确标注的合成数据。

