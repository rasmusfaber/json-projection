# Streaming projection benchmark

OS: Linux-6.17.0-41-generic-x86_64-with-glibc2.42  
CPU: AMD Ryzen 7 9800X3D 8-Core Processor  
Python: CPython 3.13.12  
Packages: json-projection 0.1.0; JSON control: Python stdlib json  
Build profile: release (operator-declared, not detected from the extension)

## Recorded run

Measured on 2026-09-07 with the release extension built from `947cffe` using Rust 1.91.1 and the harness at `b49aefd`:

```bash
uv run maturin develop --uv --release
uv run --no-sync python benchmarks/stream.py --sizes-mib 1,8,128 --chunk-kib 4,64,1024 --build-profile release
```

All 216 cases completed, with each cold call and all seven timed outputs validated. The table below
summarizes the 128 MiB binary-file cases with 64 KiB reads; the complete measurements follow.

| Input shape | Stream ms | Full-input projection ms | Stream peak RSS growth MiB | Full-input peak RSS growth MiB |
|---|---:|---:|---:|---:|
| Discarded string | 42.488 | 58.118 | 0.00 | 128.04 |
| Discarded array of small objects | 165.057 | 101.116 | 0.00 | 128.21 |
| Discarded fractional number | 34.329 | 64.072 | 0.00 | 128.30 |
| Mostly retained | 91.785 | 108.070 | 255.91 | 384.21 |

Discarding the large payload leaves 8 output bytes. Across all discarded-payload sizes, chunk sizes,
and sources, streaming peak RSS growth was at most 2.05 MiB. Zero means no increase over the process's
startup high-water mark, not zero allocations. Streaming is about 1.6 times slower than full-input
projection for the 128 MiB array case, despite avoiding the source allocation. Large string/number
cases can instead benefit from avoiding that allocation and a separate pass over the full buffer.

At 64 KiB file reads, discarded-array streaming times were 1.246, 10.334, and 165.057 ms for 1, 8, and
128 MiB respectively. Chunk-size overhead remains visible in the full table. Mostly retained input
still uses memory proportional to the output: roughly 256 MiB for a 128 MiB result, including the
temporary Python output copy. This measures one machine with warm file caches and synthetic data;
it is not a universal speedup claim or a CI performance threshold.

## Methodology

Each table row runs in a fresh child. After imports, Projection construction, and allocation of reusable source blocks, Linux ru_maxrss growth is sampled across the first call, before validation or timing. The lazy streaming plan is created inside that first call. No complete source is read or assembled before that baseline. RSS is n/a elsewhere; zero growth means no increase over the process's earlier high-water mark. Timing is the median of 7 subsequent calls, with generation or file open/read and input assembly included; validation and disposal of the returned value are outside the timer. Every output is checked for equivalent selected data. The unprojected full-parse control uses json.loads before removing unselected keys, and returns a dict; projection approaches return bytes. Output sizes are equivalent compact JSON sizes.

Generated sources reuse bounded bytes blocks. File fixtures are written lazily once per shape/size before trials; all approaches reopen them in binary mode and read at most the specified chunk size. Files and repeated reads use the ordinary OS page cache, without eviction; this is a warm-cache workload, not a cold-disk measurement. Full-input and full-parse assemble the same chunk iterator in BytesIO inside the measured call. Mostly-retained documents deliberately show the cost of buffered selected output. Use a release build for performance comparisons; build profile is recorded metadata.

| shape | source | input MiB | chunk KiB | approach | median ms | peak RSS growth MiB | output bytes |
|---|---|---:|---:|---|---:|---:|---:|
| dropped-string | generated | 1 | 4 | stream | 0.359 | 0.00 | 8 |
| dropped-string | generated | 1 | 4 | full-input | 0.280 | 1.32 | 8 |
| dropped-string | generated | 1 | 4 | full-parse | 0.947 | 2.91 | 8 |
| dropped-string | file | 1 | 4 | stream | 0.471 | 0.00 | 8 |
| dropped-string | file | 1 | 4 | full-input | 0.381 | 1.26 | 8 |
| dropped-string | file | 1 | 4 | full-parse | 0.947 | 2.77 | 8 |
| dropped-string | generated | 1 | 64 | stream | 0.271 | 0.00 | 8 |
| dropped-string | generated | 1 | 64 | full-input | 0.216 | 0.77 | 8 |
| dropped-string | generated | 1 | 64 | full-parse | 0.736 | 3.08 | 8 |
| dropped-string | file | 1 | 64 | stream | 0.297 | 0.00 | 8 |
| dropped-string | file | 1 | 64 | full-input | 0.244 | 1.42 | 8 |
| dropped-string | file | 1 | 64 | full-parse | 0.792 | 3.30 | 8 |
| dropped-string | generated | 1 | 1024 | stream | 0.270 | 0.00 | 8 |
| dropped-string | generated | 1 | 1024 | full-input | 0.216 | 1.14 | 8 |
| dropped-string | generated | 1 | 1024 | full-parse | 0.743 | 3.00 | 8 |
| dropped-string | file | 1 | 1024 | stream | 0.397 | 1.00 | 8 |
| dropped-string | file | 1 | 1024 | full-input | 0.435 | 2.00 | 8 |
| dropped-string | file | 1 | 1024 | full-parse | 0.869 | 4.00 | 8 |
| dropped-string | generated | 8 | 4 | stream | 2.275 | 0.00 | 8 |
| dropped-string | generated | 8 | 4 | full-input | 1.741 | 8.12 | 8 |
| dropped-string | generated | 8 | 4 | full-parse | 6.205 | 24.12 | 8 |
| dropped-string | file | 8 | 4 | stream | 2.977 | 0.00 | 8 |
| dropped-string | file | 8 | 4 | full-input | 2.377 | 8.32 | 8 |
| dropped-string | file | 8 | 4 | full-parse | 7.243 | 23.95 | 8 |
| dropped-string | generated | 8 | 64 | stream | 2.173 | 0.00 | 8 |
| dropped-string | generated | 8 | 64 | full-input | 1.732 | 8.07 | 8 |
| dropped-string | generated | 8 | 64 | full-parse | 6.102 | 24.05 | 8 |
| dropped-string | file | 8 | 64 | stream | 2.387 | 0.00 | 8 |
| dropped-string | file | 8 | 64 | full-input | 1.933 | 8.30 | 8 |
| dropped-string | file | 8 | 64 | full-parse | 6.492 | 24.30 | 8 |
| dropped-string | generated | 8 | 1024 | stream | 2.151 | 0.00 | 8 |
| dropped-string | generated | 8 | 1024 | full-input | 1.737 | 8.14 | 8 |
| dropped-string | generated | 8 | 1024 | full-parse | 6.084 | 24.00 | 8 |
| dropped-string | file | 8 | 1024 | stream | 2.589 | 2.05 | 8 |
| dropped-string | file | 8 | 1024 | full-input | 1.975 | 10.18 | 8 |
| dropped-string | file | 8 | 1024 | full-parse | 6.688 | 26.09 | 8 |
| dropped-string | generated | 128 | 4 | stream | 36.595 | 0.00 | 8 |
| dropped-string | generated | 128 | 4 | full-input | 48.381 | 128.08 | 8 |
| dropped-string | generated | 128 | 4 | full-parse | 122.874 | 383.89 | 8 |
| dropped-string | file | 128 | 4 | stream | 52.057 | 0.00 | 8 |
| dropped-string | file | 128 | 4 | full-input | 67.611 | 128.18 | 8 |
| dropped-string | file | 128 | 4 | full-parse | 145.921 | 383.98 | 8 |
| dropped-string | generated | 128 | 64 | stream | 34.846 | 0.00 | 8 |
| dropped-string | generated | 128 | 64 | full-input | 46.070 | 128.01 | 8 |
| dropped-string | generated | 128 | 64 | full-parse | 127.407 | 383.89 | 8 |
| dropped-string | file | 128 | 64 | stream | 42.488 | 0.00 | 8 |
| dropped-string | file | 128 | 64 | full-input | 58.118 | 128.04 | 8 |
| dropped-string | file | 128 | 64 | full-parse | 137.559 | 383.77 | 8 |
| dropped-string | generated | 128 | 1024 | stream | 34.768 | 0.00 | 8 |
| dropped-string | generated | 128 | 1024 | full-input | 45.288 | 128.00 | 8 |
| dropped-string | generated | 128 | 1024 | full-parse | 125.472 | 383.88 | 8 |
| dropped-string | file | 128 | 1024 | stream | 43.125 | 2.00 | 8 |
| dropped-string | file | 128 | 1024 | full-input | 55.630 | 130.19 | 8 |
| dropped-string | file | 128 | 1024 | full-parse | 137.758 | 386.05 | 8 |
| dropped-array | generated | 1 | 4 | stream | 1.287 | 0.00 | 8 |
| dropped-array | generated | 1 | 4 | full-input | 0.544 | 1.05 | 8 |
| dropped-array | generated | 1 | 4 | full-parse | 5.439 | 10.22 | 8 |
| dropped-array | file | 1 | 4 | stream | 1.345 | 0.00 | 8 |
| dropped-array | file | 1 | 4 | full-input | 0.629 | 1.01 | 8 |
| dropped-array | file | 1 | 4 | full-parse | 5.332 | 10.22 | 8 |
| dropped-array | generated | 1 | 64 | stream | 1.205 | 0.00 | 8 |
| dropped-array | generated | 1 | 64 | full-input | 0.572 | 1.04 | 8 |
| dropped-array | generated | 1 | 64 | full-parse | 5.131 | 10.62 | 8 |
| dropped-array | file | 1 | 64 | stream | 1.246 | 0.00 | 8 |
| dropped-array | file | 1 | 64 | full-input | 0.575 | 1.12 | 8 |
| dropped-array | file | 1 | 64 | full-parse | 5.294 | 10.55 | 8 |
| dropped-array | generated | 1 | 1024 | stream | 1.197 | 0.00 | 8 |
| dropped-array | generated | 1 | 1024 | full-input | 0.544 | 0.09 | 8 |
| dropped-array | generated | 1 | 1024 | full-parse | 5.256 | 9.66 | 8 |
| dropped-array | file | 1 | 1024 | stream | 1.348 | 0.00 | 8 |
| dropped-array | file | 1 | 1024 | full-input | 0.769 | 1.15 | 8 |
| dropped-array | file | 1 | 1024 | full-parse | 5.412 | 10.67 | 8 |
| dropped-array | generated | 8 | 4 | stream | 9.792 | 0.00 | 8 |
| dropped-array | generated | 8 | 4 | full-input | 4.491 | 7.66 | 8 |
| dropped-array | generated | 8 | 4 | full-parse | 48.576 | 84.25 | 8 |
| dropped-array | file | 8 | 4 | stream | 11.083 | 0.00 | 8 |
| dropped-array | file | 8 | 4 | full-input | 4.998 | 8.02 | 8 |
| dropped-array | file | 8 | 4 | full-parse | 50.749 | 84.45 | 8 |
| dropped-array | generated | 8 | 64 | stream | 9.811 | 0.00 | 8 |
| dropped-array | generated | 8 | 64 | full-input | 4.464 | 8.05 | 8 |
| dropped-array | generated | 8 | 64 | full-parse | 48.494 | 84.46 | 8 |
| dropped-array | file | 8 | 64 | stream | 10.334 | 0.00 | 8 |
| dropped-array | file | 8 | 64 | full-input | 4.577 | 8.16 | 8 |
| dropped-array | file | 8 | 64 | full-parse | 47.770 | 84.73 | 8 |
| dropped-array | generated | 8 | 1024 | stream | 10.050 | 0.00 | 8 |
| dropped-array | generated | 8 | 1024 | full-input | 4.471 | 8.07 | 8 |
| dropped-array | generated | 8 | 1024 | full-parse | 48.407 | 84.71 | 8 |
| dropped-array | file | 8 | 1024 | stream | 10.065 | 1.13 | 8 |
| dropped-array | file | 8 | 1024 | full-input | 4.552 | 9.23 | 8 |
| dropped-array | file | 8 | 1024 | full-parse | 48.932 | 85.61 | 8 |
| dropped-array | generated | 128 | 4 | stream | 159.729 | 0.00 | 8 |
| dropped-array | generated | 128 | 4 | full-input | 91.923 | 127.91 | 8 |
| dropped-array | generated | 128 | 4 | full-parse | 888.500 | 1352.01 | 8 |
| dropped-array | file | 128 | 4 | stream | 174.445 | 0.00 | 8 |
| dropped-array | file | 128 | 4 | full-input | 111.351 | 127.86 | 8 |
| dropped-array | file | 128 | 4 | full-parse | 948.663 | 1352.07 | 8 |
| dropped-array | generated | 128 | 64 | stream | 155.740 | 0.00 | 8 |
| dropped-array | generated | 128 | 64 | full-input | 90.120 | 127.95 | 8 |
| dropped-array | generated | 128 | 64 | full-parse | 876.039 | 1352.00 | 8 |
| dropped-array | file | 128 | 64 | stream | 165.057 | 0.00 | 8 |
| dropped-array | file | 128 | 64 | full-input | 101.116 | 128.21 | 8 |
| dropped-array | file | 128 | 64 | full-parse | 887.872 | 1352.35 | 8 |
| dropped-array | generated | 128 | 1024 | stream | 154.012 | 0.00 | 8 |
| dropped-array | generated | 128 | 1024 | full-input | 88.872 | 128.00 | 8 |
| dropped-array | generated | 128 | 1024 | full-parse | 874.729 | 1352.50 | 8 |
| dropped-array | file | 128 | 1024 | stream | 166.677 | 2.05 | 8 |
| dropped-array | file | 128 | 1024 | full-input | 101.207 | 130.07 | 8 |
| dropped-array | file | 128 | 1024 | full-parse | 892.910 | 1354.22 | 8 |
| dropped-number | generated | 1 | 4 | stream | 0.220 | 0.00 | 8 |
| dropped-number | generated | 1 | 4 | full-input | 0.419 | 1.00 | 8 |
| dropped-number | generated | 1 | 4 | full-parse | 0.868 | 3.14 | 8 |
| dropped-number | file | 1 | 4 | stream | 0.314 | 0.00 | 8 |
| dropped-number | file | 1 | 4 | full-input | 0.341 | 0.93 | 8 |
| dropped-number | file | 1 | 4 | full-parse | 0.944 | 2.86 | 8 |
| dropped-number | generated | 1 | 64 | stream | 0.204 | 0.00 | 8 |
| dropped-number | generated | 1 | 64 | full-input | 0.326 | 0.97 | 8 |
| dropped-number | generated | 1 | 64 | full-parse | 0.873 | 2.75 | 8 |
| dropped-number | file | 1 | 64 | stream | 0.244 | 0.00 | 8 |
| dropped-number | file | 1 | 64 | full-input | 0.287 | 0.91 | 8 |
| dropped-number | file | 1 | 64 | full-parse | 0.899 | 3.14 | 8 |
| dropped-number | generated | 1 | 1024 | stream | 0.206 | 0.00 | 8 |
| dropped-number | generated | 1 | 1024 | full-input | 0.262 | 1.00 | 8 |
| dropped-number | generated | 1 | 1024 | full-parse | 0.868 | 3.00 | 8 |
| dropped-number | file | 1 | 1024 | stream | 0.333 | 1.00 | 8 |
| dropped-number | file | 1 | 1024 | full-input | 0.472 | 2.14 | 8 |
| dropped-number | file | 1 | 1024 | full-parse | 1.168 | 4.00 | 8 |
| dropped-number | generated | 8 | 4 | stream | 1.772 | 0.00 | 8 |
| dropped-number | generated | 8 | 4 | full-input | 2.066 | 7.99 | 8 |
| dropped-number | generated | 8 | 4 | full-parse | 7.882 | 24.00 | 8 |
| dropped-number | file | 8 | 4 | stream | 2.470 | 0.00 | 8 |
| dropped-number | file | 8 | 4 | full-input | 2.609 | 7.94 | 8 |
| dropped-number | file | 8 | 4 | full-parse | 8.696 | 24.06 | 8 |
| dropped-number | generated | 8 | 64 | stream | 1.629 | 0.00 | 8 |
| dropped-number | generated | 8 | 64 | full-input | 2.017 | 8.14 | 8 |
| dropped-number | generated | 8 | 64 | full-parse | 7.653 | 23.92 | 8 |
| dropped-number | file | 8 | 64 | stream | 1.854 | 0.00 | 8 |
| dropped-number | file | 8 | 64 | full-input | 2.197 | 7.87 | 8 |
| dropped-number | file | 8 | 64 | full-parse | 8.126 | 24.00 | 8 |
| dropped-number | generated | 8 | 1024 | stream | 1.635 | 0.00 | 8 |
| dropped-number | generated | 8 | 1024 | full-input | 2.042 | 8.00 | 8 |
| dropped-number | generated | 8 | 1024 | full-parse | 7.080 | 24.12 | 8 |
| dropped-number | file | 8 | 1024 | stream | 2.090 | 2.05 | 8 |
| dropped-number | file | 8 | 1024 | full-input | 2.176 | 10.05 | 8 |
| dropped-number | file | 8 | 1024 | full-parse | 7.353 | 26.05 | 8 |
| dropped-number | generated | 128 | 4 | stream | 27.864 | 0.00 | 8 |
| dropped-number | generated | 128 | 4 | full-input | 56.735 | 127.78 | 8 |
| dropped-number | generated | 128 | 4 | full-parse | 158.577 | 384.10 | 8 |
| dropped-number | file | 128 | 4 | stream | 43.865 | 0.00 | 8 |
| dropped-number | file | 128 | 4 | full-input | 77.892 | 127.79 | 8 |
| dropped-number | file | 128 | 4 | full-parse | 178.865 | 384.08 | 8 |
| dropped-number | generated | 128 | 64 | stream | 26.221 | 0.00 | 8 |
| dropped-number | generated | 128 | 64 | full-input | 53.492 | 128.01 | 8 |
| dropped-number | generated | 128 | 64 | full-parse | 159.942 | 384.10 | 8 |
| dropped-number | file | 128 | 64 | stream | 34.329 | 0.00 | 8 |
| dropped-number | file | 128 | 64 | full-input | 64.072 | 128.30 | 8 |
| dropped-number | file | 128 | 64 | full-parse | 172.275 | 384.11 | 8 |
| dropped-number | generated | 128 | 1024 | stream | 26.142 | 0.00 | 8 |
| dropped-number | generated | 128 | 1024 | full-input | 55.942 | 128.00 | 8 |
| dropped-number | generated | 128 | 1024 | full-parse | 154.940 | 384.18 | 8 |
| dropped-number | file | 128 | 1024 | stream | 34.596 | 2.00 | 8 |
| dropped-number | file | 128 | 1024 | full-input | 65.551 | 130.19 | 8 |
| dropped-number | file | 128 | 1024 | full-parse | 167.911 | 386.18 | 8 |
| mostly-retained | generated | 1 | 4 | stream | 0.453 | 1.86 | 1,048,563 |
| mostly-retained | generated | 1 | 4 | full-input | 0.470 | 3.05 | 1,048,563 |
| mostly-retained | generated | 1 | 4 | full-parse | 0.719 | 2.75 | 1,048,563 |
| mostly-retained | file | 1 | 4 | stream | 0.567 | 1.81 | 1,048,563 |
| mostly-retained | file | 1 | 4 | full-input | 0.572 | 3.00 | 1,048,563 |
| mostly-retained | file | 1 | 4 | full-parse | 0.849 | 2.55 | 1,048,563 |
| mostly-retained | generated | 1 | 64 | stream | 0.448 | 2.07 | 1,048,563 |
| mostly-retained | generated | 1 | 64 | full-input | 0.460 | 3.09 | 1,048,563 |
| mostly-retained | generated | 1 | 64 | full-parse | 0.728 | 2.71 | 1,048,563 |
| mostly-retained | file | 1 | 64 | stream | 0.501 | 2.05 | 1,048,563 |
| mostly-retained | file | 1 | 64 | full-input | 0.508 | 3.21 | 1,048,563 |
| mostly-retained | file | 1 | 64 | full-parse | 0.758 | 3.12 | 1,048,563 |
| mostly-retained | generated | 1 | 1024 | stream | 0.300 | 2.00 | 1,048,563 |
| mostly-retained | generated | 1 | 1024 | full-input | 0.463 | 3.12 | 1,048,563 |
| mostly-retained | generated | 1 | 1024 | full-parse | 0.700 | 3.00 | 1,048,563 |
| mostly-retained | file | 1 | 1024 | stream | 0.559 | 3.00 | 1,048,563 |
| mostly-retained | file | 1 | 1024 | full-input | 0.582 | 4.00 | 1,048,563 |
| mostly-retained | file | 1 | 1024 | full-parse | 0.828 | 4.00 | 1,048,563 |
| mostly-retained | generated | 8 | 4 | stream | 3.753 | 16.10 | 8,388,595 |
| mostly-retained | generated | 8 | 4 | full-input | 3.921 | 23.90 | 8,388,595 |
| mostly-retained | generated | 8 | 4 | full-parse | 6.007 | 23.60 | 8,388,595 |
| mostly-retained | file | 8 | 4 | stream | 4.742 | 15.88 | 8,388,595 |
| mostly-retained | file | 8 | 4 | full-input | 4.732 | 23.81 | 8,388,595 |
| mostly-retained | file | 8 | 4 | full-parse | 6.761 | 23.88 | 8,388,595 |
| mostly-retained | generated | 8 | 64 | stream | 3.607 | 16.01 | 8,388,595 |
| mostly-retained | generated | 8 | 64 | full-input | 3.825 | 24.12 | 8,388,595 |
| mostly-retained | generated | 8 | 64 | full-parse | 5.779 | 23.87 | 8,388,595 |
| mostly-retained | file | 8 | 64 | stream | 3.930 | 16.07 | 8,388,595 |
| mostly-retained | file | 8 | 64 | full-input | 4.064 | 24.18 | 8,388,595 |
| mostly-retained | file | 8 | 64 | full-parse | 6.030 | 24.12 | 8,388,595 |
| mostly-retained | generated | 8 | 1024 | stream | 3.629 | 16.00 | 8,388,595 |
| mostly-retained | generated | 8 | 1024 | full-input | 3.767 | 24.17 | 8,388,595 |
| mostly-retained | generated | 8 | 1024 | full-parse | 5.715 | 24.00 | 8,388,595 |
| mostly-retained | file | 8 | 1024 | stream | 4.131 | 18.06 | 8,388,595 |
| mostly-retained | file | 8 | 1024 | full-input | 4.325 | 26.20 | 8,388,595 |
| mostly-retained | file | 8 | 1024 | full-parse | 6.261 | 26.06 | 8,388,595 |
| mostly-retained | generated | 128 | 4 | stream | 81.966 | 255.25 | 134,217,715 |
| mostly-retained | generated | 128 | 4 | full-input | 101.393 | 383.51 | 134,217,715 |
| mostly-retained | generated | 128 | 4 | full-parse | 125.154 | 383.93 | 134,217,715 |
| mostly-retained | file | 128 | 4 | stream | 101.352 | 255.80 | 134,217,715 |
| mostly-retained | file | 128 | 4 | full-input | 119.503 | 383.93 | 134,217,715 |
| mostly-retained | file | 128 | 4 | full-parse | 144.976 | 383.85 | 134,217,715 |
| mostly-retained | generated | 128 | 64 | stream | 79.925 | 255.84 | 134,217,715 |
| mostly-retained | generated | 128 | 64 | full-input | 95.403 | 383.92 | 134,217,715 |
| mostly-retained | generated | 128 | 64 | full-parse | 122.193 | 383.95 | 134,217,715 |
| mostly-retained | file | 128 | 64 | stream | 91.785 | 255.91 | 134,217,715 |
| mostly-retained | file | 128 | 64 | full-input | 108.070 | 384.21 | 134,217,715 |
| mostly-retained | file | 128 | 64 | full-parse | 133.651 | 384.11 | 134,217,715 |
| mostly-retained | generated | 128 | 1024 | stream | 77.794 | 256.00 | 134,217,715 |
| mostly-retained | generated | 128 | 1024 | full-input | 95.727 | 384.00 | 134,217,715 |
| mostly-retained | generated | 128 | 1024 | full-parse | 122.451 | 384.00 | 134,217,715 |
| mostly-retained | file | 128 | 1024 | stream | 91.139 | 258.05 | 134,217,715 |
| mostly-retained | file | 128 | 1024 | full-input | 108.815 | 385.94 | 134,217,715 |
| mostly-retained | file | 128 | 1024 | full-parse | 138.099 | 386.01 | 134,217,715 |
