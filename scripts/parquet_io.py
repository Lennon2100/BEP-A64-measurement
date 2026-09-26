"""Chunked Parquet I/O for the formal measurement tables.

All columns are stored as strings so downstream readers keep the same value
semantics as the earlier CSV tables.  Writing accumulates rows into bounded
row groups instead of materialising a whole batch, and reading yields one row
at a time from `pyarrow.parquet.ParquetFile.iter_batches`.
"""

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
