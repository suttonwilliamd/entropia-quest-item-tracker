"""Synthetic inventory exports (made-up Ids/quantities; no real player data)."""
import csv
import io

HEADER = ["Id", "Name", "Quantity", "Value(PED)", "Container", "ContainerRefId"]


def make_tsv(rows, *, header=HEADER, bom=True, delimiter="\t") -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, delimiter=delimiter, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    data = buf.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


BASE_ROWS = [
    ["1001", "Corria Veneer", "1500", "15.00", "CARRIED", "0"],
    ["1002", "Corria Veneer", "300", "3.00", "STORAGE (Arkadia)", "0"],
    ["1003", "Corria Veneer", "9999", "99.99", "STORAGE (Calypso)", "0"],
    ["1004", "Corria Timber", "4", "0.40", "STORAGE (Arkadia)", "0"],
    ["1005", "Soft Hide", "7", "0.07", "CARRIED", "0"],
    ["1006", "Soft Leather", "40", "4.00", "STORAGE", "0"],
    ["1007", "Animal Oil Residue", "800", "8.00", "CARRIED", "0"],
    ["1008", "Paint Can (Yellow)", "12", "1.20", "STORAGE (Arkadia Underground)", "0"],
    ["1009", "Shiny Unrelated Loot", "5", "500.00", "CARRIED", "0"],
]
