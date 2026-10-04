#!/usr/bin/env python3
"""Write a deterministic kirana catalog CSV. No DuckDB import required.

Thousands of SKUs come from crossing a small product list with brands and
pack sizes, then varying price and stock. Same seed, same file.
"""

import csv
import hashlib
import sys
from pathlib import Path

# (category, item, unit, packs as (qty, label), base rupees per pack unit)
CATALOG = [
    ("atta", "Chakki Atta", "kg", [(1, "1 kg"), (5, "5 kg"), (10, "10 kg")], 42),
    ("atta", "Multigrain Atta", "kg", [(1, "1 kg"), (5, "5 kg")], 68),
    ("atta", "Maida", "kg", [(0.5, "500 g"), (1, "1 kg")], 38),
    ("atta", "Besan", "kg", [(0.5, "500 g"), (1, "1 kg")], 90),
    ("atta", "Suji", "kg", [(0.5, "500 g"), (1, "1 kg")], 48),
    ("rice", "Basmati Rice", "kg", [(1, "1 kg"), (5, "5 kg")], 140),
    ("rice", "Sona Masoori Rice", "kg", [(1, "1 kg"), (5, "5 kg"), (10, "10 kg")], 62),
    ("rice", "Kolam Rice", "kg", [(1, "1 kg"), (5, "5 kg")], 70),
    ("rice", "Poha", "kg", [(0.5, "500 g"), (1, "1 kg")], 55),
    ("rice", "Brown Rice", "kg", [(1, "1 kg")], 110),
    ("dal", "Toor Dal", "kg", [(0.5, "500 g"), (1, "1 kg")], 150),
    ("dal", "Moong Dal", "kg", [(0.5, "500 g"), (1, "1 kg")], 140),
    ("dal", "Chana Dal", "kg", [(0.5, "500 g"), (1, "1 kg")], 95),
    ("dal", "Masoor Dal", "kg", [(0.5, "500 g"), (1, "1 kg")], 110),
    ("dal", "Urad Dal", "kg", [(0.5, "500 g"), (1, "1 kg")], 160),
    ("dal", "Rajma", "kg", [(0.5, "500 g"), (1, "1 kg")], 130),
    ("dal", "Kabuli Chana", "kg", [(0.5, "500 g"), (1, "1 kg")], 120),
    ("oil", "Mustard Oil", "L", [(1, "1 L"), (5, "5 L")], 180),
    ("oil", "Sunflower Oil", "L", [(1, "1 L"), (5, "5 L")], 150),
    ("oil", "Groundnut Oil", "L", [(1, "1 L")], 190),
    ("oil", "Ghee", "L", [(0.5, "500 ml"), (1, "1 L")], 620),
    ("oil", "Coconut Oil", "L", [(0.5, "500 ml"), (1, "1 L")], 210),
    ("spices", "Turmeric Powder", "g", [(100, "100 g"), (200, "200 g")], 0.45),
    ("spices", "Red Chilli Powder", "g", [(100, "100 g"), (200, "200 g")], 0.55),
    ("spices", "Coriander Powder", "g", [(100, "100 g"), (200, "200 g")], 0.35),
    ("spices", "Garam Masala", "g", [(50, "50 g"), (100, "100 g")], 0.9),
    ("spices", "Cumin Seeds", "g", [(100, "100 g"), (200, "200 g")], 0.7),
    ("spices", "Mustard Seeds", "g", [(100, "100 g")], 0.25),
    ("tea", "Tea Dust", "g", [(250, "250 g"), (500, "500 g"), (1000, "1 kg")], 0.45),
    ("tea", "Leaf Tea", "g", [(250, "250 g"), (500, "500 g")], 0.55),
    ("tea", "Instant Coffee", "g", [(50, "50 g"), (100, "100 g")], 4.5),
    ("tea", "Filter Coffee", "g", [(200, "200 g")], 1.8),
    ("snacks", "Glucose Biscuits", "g", [(75, "75 g"), (150, "150 g")], 0.2),
    ("snacks", "Marie Biscuits", "g", [(150, "150 g"), (300, "300 g")], 0.18),
    ("snacks", "Namkeen Mixture", "g", [(200, "200 g"), (400, "400 g")], 0.4),
    ("snacks", "Potato Chips", "g", [(50, "50 g"), (100, "100 g")], 1.2),
    ("snacks", "Instant Noodles", "g", [(70, "70 g"), (280, "4-pack")], 0.25),
    ("snacks", "Papad", "g", [(200, "200 g")], 0.55),
    ("soap", "Bath Soap", "g", [(75, "75 g"), (125, "125 g")], 0.4),
    ("soap", "Detergent Bar", "g", [(150, "150 g"), (250, "250 g")], 0.15),
    ("soap", "Detergent Powder", "kg", [(0.5, "500 g"), (1, "1 kg"), (2, "2 kg")], 90),
    ("soap", "Dishwash Bar", "g", [(200, "200 g"), (500, "500 g")], 0.12),
    ("soap", "Toothpaste", "g", [(100, "100 g"), (200, "200 g")], 1.1),
    ("soap", "Hair Oil", "ml", [(100, "100 ml"), (200, "200 ml")], 1.6),
    ("dairy", "Toned Milk", "L", [(0.5, "500 ml"), (1, "1 L")], 54),
    ("dairy", "Full Cream Milk", "L", [(0.5, "500 ml"), (1, "1 L")], 66),
    ("dairy", "Curd", "kg", [(0.4, "400 g"), (1, "1 kg")], 70),
    ("dairy", "Paneer", "g", [(200, "200 g")], 0.45),
    ("dairy", "Butter", "g", [(100, "100 g"), (500, "500 g")], 0.55),
    ("staples", "Sugar", "kg", [(1, "1 kg"), (5, "5 kg")], 44),
    ("staples", "Iodised Salt", "kg", [(1, "1 kg")], 22),
    ("staples", "Jaggery", "kg", [(0.5, "500 g"), (1, "1 kg")], 60),
    ("staples", "Poha Thick", "kg", [(0.5, "500 g")], 48),
    ("beverages", "Mango Drink", "ml", [(200, "200 ml"), (1000, "1 L")], 0.08),
    ("beverages", "Cola", "ml", [(750, "750 ml"), (2000, "2 L")], 0.06),
    ("beverages", "Packaged Water", "L", [(1, "1 L"), (2, "2 L")], 20),
    ("beverages", "Glucon-D", "g", [(500, "500 g"), (1000, "1 kg")], 0.35),
    ("personal", "Shampoo Sachet", "ml", [(6, "6 ml"), (180, "180 ml")], 0.8),
    ("personal", "Face Cream", "g", [(50, "50 g")], 2.4),
    ("personal", "Antiseptic Liquid", "ml", [(100, "100 ml"), (500, "500 ml")], 0.4),
    ("household", "Phenyl", "ml", [(500, "500 ml"), (1000, "1 L")], 0.09),
    ("household", "Floor Cleaner", "L", [(1, "1 L")], 140),
    ("household", "Mosquito Coil", "pc", [(10, "10 pc")], 6),
    ("household", "Incense Sticks", "pc", [(1, "1 pack")], 25),
    ("household", "Matchbox", "pc", [(10, "10 box")], 2),
    ("pickles", "Mango Pickle", "g", [(300, "300 g"), (500, "500 g")], 0.28),
    ("pickles", "Mixed Pickle", "g", [(300, "300 g")], 0.26),
    ("dryfruit", "Cashew", "g", [(100, "100 g"), (250, "250 g")], 8.5),
    ("dryfruit", "Almond", "g", [(100, "100 g"), (250, "250 g")], 9.0),
    ("dryfruit", "Raisin", "g", [(100, "100 g"), (250, "250 g")], 3.2),
]

# Repeated so the cross product lands in the thousands without inventing
# fake categories. Variant is part of the SKU identity, not a separate item.
VARIANTS = ["", "Family Pack", "Economy", "Value", "Extra"]

BRANDS = {
    "atta": ["Aashirvaad", "Pillsbury", "Fortune", "Patanjali", "Local Chakki"],
    "rice": ["India Gate", "Daawat", "Fortune", "Kohinoor", "Lal Qilla"],
    "dal": ["Tata Sampann", "Fortune", "Organic Tattva", "24 Mantra", "Local"],
    "oil": ["Fortune", "Saffola", "Amul", "Dhara", "Patanjali"],
    "spices": ["MDH", "Everest", "Catch", "Badshah", "Eastern"],
    "tea": ["Tata Tea", "Red Label", "Nescafe", "Bru", "Society"],
    "snacks": ["Parle-G", "Britannia", "Haldiram", "Maggi", "Lays"],
    "soap": ["Lifebuoy", "Surf Excel", "Vim", "Colgate", "Parachute"],
    "dairy": ["Amul", "Mother Dairy", "Nandini", "Verka", "Local Dairy"],
    "staples": ["Tata", "Local", "24 Mantra", "Fortune"],
    "beverages": ["Frooti", "Thums Up", "Bisleri", "Glucon-D"],
    "personal": ["Clinic Plus", "Fair & Lovely", "Dettol", "Sunsilk"],
    "household": ["Lizol", "Good Knight", "Cycle", "Local"],
    "pickles": ["Mother's Recipe", "Priya", "Local"],
    "dryfruit": ["Tulsi", "Local", "24 Mantra"],
}


def unit_noise(key: str) -> int:
    digest = hashlib.sha256(key.encode()).hexdigest()
    return int(digest[:8], 16)


def rows():
    seen = set()
    for category, item, unit, packs, base in CATALOG:
        for brand in BRANDS[category]:
            for variant in VARIANTS:
                for qty, label in packs:
                    key = f"{brand}|{variant}|{item}|{label}"
                    noise = unit_noise(key)
                    sku = f"{category[:3].upper()}-{noise % 100000:05d}"
                    if sku in seen:
                        sku = f"{sku}-{noise % 97:02d}"
                    seen.add(sku)
                    # Economy/Value sit a bit under the shelf price.
                    variant_cut = 0.92 if variant in ("Economy", "Value") else 1.0
                    factor = (0.88 + (noise % 25) / 100) * variant_cut
                    price = max(100, int(round(base * qty * factor * 100)))
                    mrp = int(price * (1.04 + (noise % 8) / 100))
                    stock_roll = noise % 100
                    if stock_roll < 8:
                        stock = 0
                    elif stock_roll < 18:
                        stock = 1 + noise % 3
                    else:
                        stock = 4 + noise % 40
                    display = f"{brand} {item}"
                    if variant:
                        display = f"{display} {variant}"
                    yield {
                        "sku": sku,
                        "name": display,
                        "brand": brand,
                        "category": category,
                        "unit": unit,
                        "pack_qty": qty,
                        "pack_label": label,
                        "price_paise": price,
                        "mrp_paise": mrp,
                        "stock": stock,
                    }


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "products.csv")
    fieldnames = [
        "sku",
        "name",
        "brand",
        "category",
        "unit",
        "pack_qty",
        "pack_label",
        "price_paise",
        "mrp_paise",
        "stock",
    ]
    catalog = list(rows())
    with out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(catalog)
    print(f"wrote {len(catalog)} products to {out}")


if __name__ == "__main__":
    main()
