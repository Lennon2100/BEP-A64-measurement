"""Chunked Parquet I/O for the formal measurement tables.

All columns are stored as strings so downstream readers keep the same value
semantics as the earlier CSV tables.  Writing accumulates rows into bounded
row groups instead of materialising a whole batch, and reading yields one row
at a time from `pyarrow.parquet.ParquetFile.iter_batches`.
"""

import csv
import io

import pyarrow as pa
import pyarrow.parquet as pq

ROW_GROUP = 200_000


class StringParquetWriter:
    """Write dict rows to one Parquet file in bounded-memory row groups."""

    def __init__(self, path, fields):
        self.schema = pa.schema([(name, pa.string()) for name in fields])
        self.writer = pq.ParquetWriter(path, self.schema)
        self.buffer = []

    def write(self, row):
        self.buffer.append(
            {key: "" if value is None else str(value) for key, value in row.items()}
        )
        if len(self.buffer) >= ROW_GROUP:
            self._flush()

    def _flush(self):
        if self.buffer:
            self.writer.write_table(pa.Table.from_pylist(self.buffer, schema=self.schema))
            self.buffer.clear()

    def close(self):
        self._flush()
        self.writer.close()


def iter_parquet_rows(source):
    """Yield dict rows from a Parquet path or seekable file-like object."""
    for batch in pq.ParquetFile(source).iter_batches(batch_size=ROW_GROUP):
        yield from batch.to_pylist()


def iter_archive_table(bundle, parquet_name, csv_name):
    """Yield dict rows from an archive's Parquet member, or its legacy CSV."""
    names = bundle.getnames()
    if parquet_name in names:
        with bundle.extractfile(parquet_name) as raw:
            data = raw.read()
        yield from iter_parquet_rows(io.BytesIO(data))
        return
    if csv_name in names:
        with bundle.extractfile(csv_name) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
            yield from reader
        return
    raise KeyError(f"archive has neither {parquet_name!r} nor {csv_name!r}")


def iter_table_file(parquet_path, csv_path):
    """Yield dict rows from a Parquet file, or its legacy CSV."""
    if parquet_path.is_file():
        yield from iter_parquet_rows(parquet_path)
        return
    if csv_path.is_file():
        with open(csv_path, newline="", encoding="utf-8") as fh:
            yield from csv.DictReader(fh)
        return
    raise FileNotFoundError(parquet_path)
