#!/usr/bin/env python3
"""Write a deterministic grocery catalog CSV. No DuckDB import required.

Thousands of SKUs come from crossing a small product list with brands and
pack sizes, then varying price and stock. Same seed, same file.
"""

import csv
import hashlib
import sys
from pathlib import Path

# (category, item, unit, packs as (qty, label), base dollars per pack unit)
CATALOG = [
    ("flour", "All-Purpose Flour", "kg", [(1, "1 kg"), (2.5, "2.5 kg"), (5, "5 kg")], 1.6),
    ("flour", "Whole Wheat Flour", "kg", [(1, "1 kg"), (2.5, "2.5 kg")], 2.0),
    ("flour", "Bread Flour", "kg", [(1, "1 kg")], 2.3),
    ("flour", "Cornmeal", "kg", [(0.5, "500 g"), (1, "1 kg")], 2.6),
    ("flour", "Pancake Mix", "g", [(500, "500 g")], 0.007),
    ("grains", "Long Grain Rice", "kg", [(1, "1 kg"), (5, "5 kg"), (10, "10 kg")], 2.4),
    ("grains", "Basmati Rice", "kg", [(1, "1 kg"), (5, "5 kg")], 4.2),
    ("grains", "Jasmine Rice", "kg", [(1, "1 kg"), (5, "5 kg")], 3.8),
    ("grains", "Brown Rice", "kg", [(1, "1 kg")], 3.2),
    ("grains", "Rolled Oats", "kg", [(0.5, "500 g"), (1, "1 kg")], 3.0),
    ("grains", "Quinoa", "g", [(500, "500 g")], 0.009),
    ("pasta", "Spaghetti", "g", [(500, "500 g"), (1000, "1 kg")], 0.003),
    ("pasta", "Penne", "g", [(500, "500 g")], 0.003),
    ("pasta", "Macaroni", "g", [(500, "500 g")], 0.0028),
    ("pasta", "Egg Noodles", "g", [(400, "400 g")], 0.005),
    ("pasta", "Instant Noodles", "g", [(85, "85 g"), (425, "5-pack")], 0.008),
    ("legumes", "Red Lentils", "kg", [(0.5, "500 g"), (1, "1 kg")], 4.0),
    ("legumes", "Green Lentils", "kg", [(0.5, "500 g"), (1, "1 kg")], 4.4),
    ("legumes", "Dried Chickpeas", "kg", [(0.5, "500 g"), (1, "1 kg")], 3.6),
    ("legumes", "Canned Black Beans", "g", [(400, "400 g")], 0.003),
    ("legumes", "Canned Kidney Beans", "g", [(400, "400 g")], 0.003),
    ("legumes", "Baked Beans", "g", [(415, "415 g")], 0.0035),
    ("oil", "Olive Oil", "L", [(0.5, "500 ml"), (1, "1 L")], 9.0),
    ("oil", "Extra Virgin Olive Oil", "L", [(0.5, "500 ml"), (1, "1 L")], 12.0),
    ("oil", "Sunflower Oil", "L", [(1, "1 L"), (3, "3 L")], 3.4),
    ("oil", "Canola Oil", "L", [(1, "1 L"), (3, "3 L")], 3.1),
    ("oil", "Coconut Oil", "ml", [(400, "400 ml")], 0.016),
    ("oil", "Vinegar", "ml", [(500, "500 ml")], 0.004),
    ("condiments", "Ketchup", "g", [(500, "500 g"), (1000, "1 kg")], 0.006),
    ("condiments", "Mayonnaise", "g", [(400, "400 g")], 0.009),
    ("condiments", "Mustard", "g", [(250, "250 g")], 0.009),
    ("condiments", "Peanut Butter", "g", [(340, "340 g"), (1000, "1 kg")], 0.011),
    ("condiments", "Strawberry Jam", "g", [(340, "340 g")], 0.011),
    ("condiments", "Honey", "g", [(350, "350 g")], 0.02),
    ("condiments", "Soy Sauce", "ml", [(250, "250 ml")], 0.012),
    ("spices", "Sea Salt", "g", [(500, "500 g"), (1000, "1 kg")], 0.0018),
    ("spices", "Black Pepper", "g", [(50, "50 g"), (100, "100 g")], 0.06),
    ("spices", "Ground Cinnamon", "g", [(50, "50 g")], 0.07),
    ("spices", "Paprika", "g", [(50, "50 g")], 0.06),
    ("spices", "Ground Cumin", "g", [(50, "50 g")], 0.06),
    ("spices", "Chili Flakes", "g", [(50, "50 g")], 0.06),
    ("coffee", "Ground Coffee", "g", [(250, "250 g"), (500, "500 g")], 0.018),
    ("coffee", "Coffee Beans", "g", [(500, "500 g"), (1000, "1 kg")], 0.02),
    ("coffee", "Instant Coffee", "g", [(50, "50 g"), (100, "100 g"), (200, "200 g")], 0.045),
    ("coffee", "Black Tea Bags", "pc", [(40, "40 bags"), (80, "80 bags")], 0.06),
    ("coffee", "Green Tea Bags", "pc", [(25, "25 bags")], 0.12),
    ("coffee", "Herbal Tea Bags", "pc", [(20, "20 bags")], 0.15),
    ("breakfast", "Corn Flakes", "g", [(500, "500 g"), (750, "750 g")], 0.008),
    ("breakfast", "Granola", "g", [(500, "500 g")], 0.012),
    ("breakfast", "Muesli", "g", [(750, "750 g")], 0.009),
    ("snacks", "Potato Chips", "g", [(50, "50 g"), (150, "150 g")], 0.02),
    ("snacks", "Tortilla Chips", "g", [(200, "200 g")], 0.015),
    ("snacks", "Chocolate Chip Cookies", "g", [(200, "200 g"), (400, "400 g")], 0.014),
    ("snacks", "Digestive Biscuits", "g", [(400, "400 g")], 0.006),
    ("snacks", "Crackers", "g", [(200, "200 g")], 0.014),
    ("snacks", "Milk Chocolate Bar", "g", [(100, "100 g"), (200, "200 g")], 0.025),
    ("snacks", "Salted Peanuts", "g", [(200, "200 g")], 0.012),
    ("snacks", "Microwave Popcorn", "pc", [(3, "3-pack")], 1.2),
    ("dairy", "Whole Milk", "L", [(1, "1 L"), (2, "2 L")], 1.3),
    ("dairy", "Skim Milk", "L", [(1, "1 L")], 1.2),
    ("dairy", "Greek Yogurt", "g", [(500, "500 g"), (1000, "1 kg")], 0.007),
    ("dairy", "Yogurt Cups", "pc", [(4, "4 x 125 g")], 0.8),
    ("dairy", "Cheddar Cheese", "g", [(200, "200 g"), (400, "400 g")], 0.016),
    ("dairy", "Mozzarella", "g", [(125, "125 g")], 0.016),
    ("dairy", "Cream Cheese", "g", [(200, "200 g")], 0.014),
    ("dairy", "Butter", "g", [(250, "250 g"), (500, "500 g")], 0.014),
    ("dairy", "Eggs", "pc", [(6, "6 eggs"), (12, "12 eggs")], 0.35),
    ("bakery", "White Bread", "pc", [(1, "800 g loaf")], 2.4),
    ("bakery", "Whole Wheat Bread", "pc", [(1, "800 g loaf")], 2.9),
    ("bakery", "Bagels", "pc", [(4, "4-pack")], 0.8),
    ("bakery", "Flour Tortillas", "pc", [(8, "8-pack")], 0.35),
    ("canned", "Chopped Tomatoes", "g", [(400, "400 g")], 0.0035),
    ("canned", "Tuna in Water", "g", [(160, "160 g")], 0.012),
    ("canned", "Sweetcorn", "g", [(340, "340 g")], 0.004),
    ("canned", "Coconut Milk", "ml", [(400, "400 ml")], 0.005),
    ("canned", "Tomato Soup", "g", [(400, "400 g")], 0.005),
    ("baking", "White Sugar", "kg", [(1, "1 kg"), (2, "2 kg")], 1.6),
    ("baking", "Brown Sugar", "kg", [(1, "1 kg")], 2.2),
    ("baking", "Baking Powder", "g", [(200, "200 g")], 0.012),
    ("baking", "Raisins", "g", [(250, "250 g")], 0.014),
    ("baking", "Almonds", "g", [(200, "200 g"), (500, "500 g")], 0.025),
    ("beverages", "Cola", "ml", [(330, "330 ml can"), (2000, "2 L")], 0.0012),
    ("beverages", "Orange Juice", "L", [(1, "1 L"), (2, "2 L")], 3.0),
    ("beverages", "Sparkling Water", "L", [(1, "1 L")], 1.4),
    ("beverages", "Still Water", "ml", [(500, "500 ml"), (1500, "1.5 L"), (9000, "6 x 1.5 L")], 0.0007),
    ("beverages", "Sports Drink", "ml", [(500, "500 ml")], 0.004),
    ("cleaning", "Laundry Detergent", "L", [(1, "1 L"), (2, "2 L"), (4, "4 L")], 5.5),
    ("cleaning", "Dish Soap", "ml", [(500, "500 ml"), (1000, "1 L")], 0.006),
    ("cleaning", "Bleach", "L", [(1, "1 L")], 2.5),
    ("cleaning", "All-Purpose Cleaner", "ml", [(750, "750 ml")], 0.005),
    ("cleaning", "Paper Towels", "pc", [(2, "2 rolls"), (6, "6 rolls")], 1.3),
    ("cleaning", "Trash Bags", "pc", [(20, "20 bags")], 0.25),
    ("cleaning", "Sponges", "pc", [(3, "3-pack")], 0.9),
    ("personal", "Bar Soap", "pc", [(1, "1 bar"), (4, "4 bars")], 1.4),
    ("personal", "Toothpaste", "ml", [(75, "75 ml"), (125, "125 ml")], 0.035),
    ("personal", "Shampoo", "ml", [(50, "50 ml travel"), (250, "250 ml"), (500, "500 ml")], 0.016),
    ("personal", "Deodorant", "ml", [(150, "150 ml")], 0.03),
    ("personal", "Toilet Paper", "pc", [(4, "4 rolls"), (12, "12 rolls")], 0.55),
    ("personal", "Hand Soap", "ml", [(250, "250 ml")], 0.012),
]

# Repeated so the cross product lands in the thousands without inventing
# fake categories. Variant is part of the SKU identity, not a separate item.
VARIANTS = ["", "Family Pack", "Economy", "Value", "Organic"]

BRANDS = {
    "flour": ["King Arthur", "Gold Medal", "Bob's Red Mill", "Pillsbury", "Market Basics"],
    "grains": ["Tilda", "Mahatma", "Lundberg", "Riceland", "Market Basics"],
    "pasta": ["Barilla", "De Cecco", "Ronzoni", "Rummo", "Market Basics"],
    "legumes": ["Goya", "Bush's", "Heinz", "Bob's Red Mill", "Market Basics"],
    "oil": ["Bertolli", "Filippo Berio", "Colavita", "Market Basics"],
    "condiments": ["Heinz", "Hellmann's", "Kikkoman", "Smucker's", "Market Basics"],
    "spices": ["McCormick", "Schwartz", "Badia", "Simply Organic", "Market Basics"],
    "coffee": ["Lavazza", "Nescafe", "Starbucks", "Twinings", "Lipton"],
    "breakfast": ["Kellogg's", "Quaker", "Nature Valley", "Market Basics"],
    "snacks": ["Lay's", "McVitie's", "Nabisco", "Cadbury", "Planters"],
    "dairy": ["Arla", "Danone", "President", "Kerrygold", "Market Basics"],
    "bakery": ["Warburtons", "Dave's Killer Bread", "Thomas'", "Mission", "Market Basics"],
    "canned": ["Heinz", "Campbell's", "Del Monte", "Mutti", "Market Basics"],
    "baking": ["Domino", "Tate & Lyle", "Arm & Hammer", "Blue Diamond", "Market Basics"],
    "beverages": ["Coca-Cola", "Pepsi", "Tropicana", "San Pellegrino", "Evian"],
    "cleaning": ["Tide", "Persil", "Dawn", "Clorox", "Bounty"],
    "personal": ["Dove", "Colgate", "Pantene", "Head & Shoulders", "Charmin"],
}


# Where the category list would put a brand on something it never makes.
# Store brand last, so every item has a cheap row.
ITEM_BRANDS = {
    "Cornmeal": ["Bob's Red Mill", "Quaker", "Market Basics"],
    "Pancake Mix": ["Pearl Milling", "Bisquick", "Market Basics"],
    "Rolled Oats": ["Quaker", "Bob's Red Mill", "Market Basics"],
    "Quinoa": ["Bob's Red Mill", "Lundberg", "Market Basics"],
    "Egg Noodles": ["Barilla", "Ronzoni", "Market Basics"],
    "Instant Noodles": ["Nissin", "Maruchan", "Indomie", "Market Basics"],
    "Red Lentils": ["Goya", "Bob's Red Mill", "Market Basics"],
    "Green Lentils": ["Goya", "Bob's Red Mill", "Market Basics"],
    "Dried Chickpeas": ["Goya", "Bob's Red Mill", "Market Basics"],
    "Canned Black Beans": ["Goya", "Bush's", "Market Basics"],
    "Canned Kidney Beans": ["Goya", "Bush's", "Market Basics"],
    "Baked Beans": ["Heinz", "Bush's", "Market Basics"],
    "Sunflower Oil": ["Mazola", "Market Basics"],
    "Canola Oil": ["Crisco", "Mazola", "Market Basics"],
    "Coconut Oil": ["Nutiva", "Market Basics"],
    "Vinegar": ["Heinz", "Bragg", "Market Basics"],
    "Ketchup": ["Heinz", "Hunt's", "Market Basics"],
    "Mayonnaise": ["Hellmann's", "Heinz", "Market Basics"],
    "Mustard": ["French's", "Heinz", "Market Basics"],
    "Peanut Butter": ["Skippy", "Jif", "Smucker's", "Market Basics"],
    "Strawberry Jam": ["Smucker's", "Bonne Maman", "Market Basics"],
    "Honey": ["Rowse", "Market Basics"],
    "Soy Sauce": ["Kikkoman", "Market Basics"],
    "Ground Coffee": ["Lavazza", "Starbucks", "Market Basics"],
    "Coffee Beans": ["Lavazza", "Starbucks", "Market Basics"],
    "Instant Coffee": ["Nescafe", "Starbucks", "Market Basics"],
    "Black Tea Bags": ["Twinings", "Lipton", "PG Tips", "Market Basics"],
    "Green Tea Bags": ["Twinings", "Lipton", "Market Basics"],
    "Herbal Tea Bags": ["Twinings", "Celestial", "Market Basics"],
    "Potato Chips": ["Lay's", "Pringles", "Market Basics"],
    "Tortilla Chips": ["Tostitos", "Doritos", "Market Basics"],
    "Chocolate Chip Cookies": ["Chips Ahoy!", "McVitie's", "Market Basics"],
    "Digestive Biscuits": ["McVitie's", "Market Basics"],
    "Crackers": ["Ritz", "Triscuit", "Market Basics"],
    "Milk Chocolate Bar": ["Cadbury", "Hershey's", "Lindt", "Market Basics"],
    "Salted Peanuts": ["Planters", "Market Basics"],
    "Microwave Popcorn": ["Orville Redenbacher's", "Pop Secret", "Market Basics"],
    "Greek Yogurt": ["Fage", "Chobani", "Danone", "Market Basics"],
    "Yogurt Cups": ["Danone", "Yoplait", "Market Basics"],
    "Cheddar Cheese": ["Cabot", "Tillamook", "Market Basics"],
    "Mozzarella": ["Galbani", "President", "Market Basics"],
    "Cream Cheese": ["Philadelphia", "Market Basics"],
    "Butter": ["Kerrygold", "President", "Arla", "Market Basics"],
    "Eggs": ["Eggland's Best", "Pete and Gerry's", "Market Basics"],
    "Whole Milk": ["Arla", "Horizon", "Market Basics"],
    "Skim Milk": ["Arla", "Horizon", "Market Basics"],
    "Bagels": ["Thomas'", "Market Basics"],
    "Flour Tortillas": ["Mission", "Old El Paso", "Market Basics"],
    "White Bread": ["Warburtons", "Wonder", "Market Basics"],
    "Whole Wheat Bread": ["Warburtons", "Dave's Killer Bread", "Market Basics"],
    "Chopped Tomatoes": ["Mutti", "Del Monte", "Market Basics"],
    "Tuna in Water": ["StarKist", "John West", "Market Basics"],
    "Sweetcorn": ["Green Giant", "Del Monte", "Market Basics"],
    "Coconut Milk": ["Thai Kitchen", "Market Basics"],
    "Tomato Soup": ["Campbell's", "Heinz", "Market Basics"],
    "White Sugar": ["Domino", "Tate & Lyle", "Market Basics"],
    "Brown Sugar": ["Domino", "Tate & Lyle", "Market Basics"],
    "Baking Powder": ["Clabber Girl", "Arm & Hammer", "Market Basics"],
    "Raisins": ["Sun-Maid", "Market Basics"],
    "Almonds": ["Blue Diamond", "Market Basics"],
    "Cola": ["Coca-Cola", "Pepsi", "Market Basics"],
    "Orange Juice": ["Tropicana", "Minute Maid", "Market Basics"],
    "Sparkling Water": ["San Pellegrino", "Perrier", "Market Basics"],
    "Still Water": ["Evian", "Dasani", "Market Basics"],
    "Sports Drink": ["Gatorade", "Powerade", "Market Basics"],
    "Laundry Detergent": ["Tide", "Persil", "Market Basics"],
    "Dish Soap": ["Dawn", "Fairy", "Market Basics"],
    "Bleach": ["Clorox", "Domestos", "Market Basics"],
    "All-Purpose Cleaner": ["Lysol", "Mr. Clean", "Market Basics"],
    "Paper Towels": ["Bounty", "Market Basics"],
    "Trash Bags": ["Glad", "Hefty", "Market Basics"],
    "Sponges": ["Scotch-Brite", "Market Basics"],
    "Bar Soap": ["Dove", "Irish Spring", "Market Basics"],
    "Toothpaste": ["Colgate", "Crest", "Sensodyne", "Market Basics"],
    "Shampoo": ["Pantene", "Head & Shoulders", "Dove", "Market Basics"],
    "Deodorant": ["Dove", "Old Spice", "Market Basics"],
    "Toilet Paper": ["Charmin", "Cottonelle", "Market Basics"],
    "Hand Soap": ["Softsoap", "Dove", "Market Basics"],
    "Corn Flakes": ["Kellogg's", "Market Basics"],
    "Granola": ["Nature Valley", "Quaker", "Market Basics"],
    "Muesli": ["Alpen", "Market Basics"],
}

def unit_noise(key: str) -> int:
    digest = hashlib.sha256(key.encode()).hexdigest()
    return int(digest[:8], 16)


def rows():
    seen = set()
    for category, item, unit, packs, base in CATALOG:
        for brand in ITEM_BRANDS.get(item, BRANDS[category]):
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
                    price = max(49, int(round(base * qty * factor * 100)))
                    list_price = int(price * (1.04 + (noise % 8) / 100))
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
                        "price_cents": price,
                        "list_cents": list_price,
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
        "price_cents",
        "list_cents",
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
