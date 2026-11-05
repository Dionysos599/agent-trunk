"""Reference solutions for the ten tasks of the demo board (used by simulate.py and the tests).

The agents you try the demo with never see this file; it exists so that the demo itself can be checked:
if scripted agents can finish the board, a real one can be judged against the same result.
"""

# task id -> (module, function description for the task text, module source, test source, changelog text)
TASKS = {}


def task(tid, module, spec, source, test, changelog):
    TASKS[tid] = {"module": module, "spec": spec, "source": source, "test": test, "changelog": changelog}


task("prices", "prices", "apply_discount(price, percent) returns price reduced by that percentage, rounded to 2 "
     "decimals; it raises ValueError if price is negative or percent is outside 0..100",
     '''def apply_discount(price, percent):
    if price < 0 or not 0 <= percent <= 100:
        raise ValueError("bad price or percent")
    return round(price * (1 - percent / 100), 2)
''',
     '''import unittest

from prices import apply_discount


class PricesTests(unittest.TestCase):
    def test_ten_percent(self):
        self.assertEqual(apply_discount(200, 10), 180.0)

    def test_zero_and_hundred(self):
        self.assertEqual(apply_discount(50, 0), 50)
        self.assertEqual(apply_discount(50, 100), 0)

    def test_rounding(self):
        self.assertEqual(apply_discount(19.99, 15), 16.99)

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            apply_discount(-1, 10)
        with self.assertRaises(ValueError):
            apply_discount(10, 101)
''', "percentage discounts")

task("tax", "tax", "add_tax(amount, rate) returns amount plus tax at that rate (0.2 means 20 percent), rounded to 2 "
     "decimals; it raises ValueError if the rate is negative",
     '''def add_tax(amount, rate):
    if rate < 0:
        raise ValueError("negative rate")
    return round(amount * (1 + rate), 2)
''',
     '''import unittest

from tax import add_tax


class TaxTests(unittest.TestCase):
    def test_twenty_percent(self):
        self.assertEqual(add_tax(100, 0.2), 120.0)

    def test_zero_rate(self):
        self.assertEqual(add_tax(80, 0), 80)

    def test_rounding(self):
        self.assertEqual(add_tax(9.99, 0.07), 10.69)

    def test_negative_rate(self):
        with self.assertRaises(ValueError):
            add_tax(10, -0.1)
''', "sales tax")

task("cart", "cart", "class Cart with add(name, price, qty=1), remove(name), items() returning a list of "
     "(name, price, qty) tuples in the order added, and total() returning the sum of price*qty; adding a name that "
     "is already in the cart increases its quantity; remove raises KeyError for an unknown name",
     '''class Cart:
    def __init__(self):
        self._lines = {}

    def add(self, name, price, qty=1):
        if name in self._lines:
            old = self._lines[name]
            self._lines[name] = (name, old[1], old[2] + qty)
        else:
            self._lines[name] = (name, price, qty)

    def remove(self, name):
        del self._lines[name]

    def items(self):
        return list(self._lines.values())

    def total(self):
        return sum(price * qty for _, price, qty in self._lines.values())
''',
     '''import unittest

from cart import Cart


class CartTests(unittest.TestCase):
    def test_total(self):
        c = Cart()
        c.add("pen", 2.5, 4)
        c.add("book", 10)
        self.assertEqual(c.total(), 20.0)

    def test_same_name_adds_quantity(self):
        c = Cart()
        c.add("pen", 2, 1)
        c.add("pen", 2, 2)
        self.assertEqual(c.items(), [("pen", 2, 3)])

    def test_remove(self):
        c = Cart()
        c.add("pen", 2)
        c.remove("pen")
        self.assertEqual(c.items(), [])
        with self.assertRaises(KeyError):
            c.remove("pen")
''', "shopping cart")

task("inventory", "inventory", "class Inventory with add_stock(name, qty), in_stock(name, qty=1) returning a bool, and "
     "remove_stock(name, qty) which raises ValueError if there is not enough stock",
     '''class Inventory:
    def __init__(self):
        self._stock = {}

    def add_stock(self, name, qty):
        self._stock[name] = self._stock.get(name, 0) + qty

    def in_stock(self, name, qty=1):
        return self._stock.get(name, 0) >= qty

    def remove_stock(self, name, qty):
        if not self.in_stock(name, qty):
            raise ValueError("not enough stock")
        self._stock[name] -= qty
''',
     '''import unittest

from inventory import Inventory


class InventoryTests(unittest.TestCase):
    def test_in_stock(self):
        inv = Inventory()
        inv.add_stock("pen", 3)
        self.assertTrue(inv.in_stock("pen"))
        self.assertTrue(inv.in_stock("pen", 3))
        self.assertFalse(inv.in_stock("pen", 4))
        self.assertFalse(inv.in_stock("ink"))

    def test_remove(self):
        inv = Inventory()
        inv.add_stock("pen", 3)
        inv.remove_stock("pen", 2)
        self.assertFalse(inv.in_stock("pen", 2))

    def test_remove_too_many(self):
        inv = Inventory()
        with self.assertRaises(ValueError):
            inv.remove_stock("pen", 1)
''', "stock tracking")

task("shipping", "shipping", "shipping_cost(weight_kg) returns 5.0 up to and including 1 kg, 8.5 above 1 kg up to and "
     "including 5 kg, and above 5 kg 8.5 plus 1.2 for every started kilogram over 5; it raises ValueError for a "
     "negative weight",
     '''import math


def shipping_cost(weight_kg):
    if weight_kg < 0:
        raise ValueError("negative weight")
    if weight_kg <= 1:
        return 5.0
    if weight_kg <= 5:
        return 8.5
    return round(8.5 + 1.2 * math.ceil(weight_kg - 5), 2)
''',
     '''import unittest

from shipping import shipping_cost


class ShippingTests(unittest.TestCase):
    def test_light(self):
        self.assertEqual(shipping_cost(0.5), 5.0)
        self.assertEqual(shipping_cost(1), 5.0)

    def test_medium(self):
        self.assertEqual(shipping_cost(1.1), 8.5)
        self.assertEqual(shipping_cost(5), 8.5)

    def test_heavy(self):
        self.assertEqual(shipping_cost(5.1), 9.7)
        self.assertEqual(shipping_cost(8), 12.1)

    def test_negative(self):
        with self.assertRaises(ValueError):
            shipping_cost(-1)
''', "shipping costs")

task("coupons", "coupons", "apply_coupon(total, code) where the code SAVE10 takes 10 percent off and FIVEOFF takes 5 "
     "off, never going below 0, rounded to 2 decimals; an unknown code raises ValueError",
     '''def apply_coupon(total, code):
    if code == "SAVE10":
        return round(max(0.0, total * 0.9), 2)
    if code == "FIVEOFF":
        return round(max(0.0, total - 5), 2)
    raise ValueError("unknown coupon")
''',
     '''import unittest

from coupons import apply_coupon


class CouponTests(unittest.TestCase):
    def test_percent(self):
        self.assertEqual(apply_coupon(50, "SAVE10"), 45.0)

    def test_flat(self):
        self.assertEqual(apply_coupon(50, "FIVEOFF"), 45.0)

    def test_never_negative(self):
        self.assertEqual(apply_coupon(3, "FIVEOFF"), 0.0)

    def test_unknown(self):
        with self.assertRaises(ValueError):
            apply_coupon(10, "NOPE")
''', "coupon codes")

task("receipt", "receipt", "format_receipt(lines, total) where lines is a list of (name, price, qty) tuples returns "
     "a string with one line per item in the form '<name> x<qty> <price*qty with 2 decimals>' followed by a last "
     "line 'TOTAL <total with 2 decimals>', lines separated by newlines",
     '''def format_receipt(lines, total):
    rows = [f"{name} x{qty} {price * qty:.2f}" for name, price, qty in lines]
    rows.append(f"TOTAL {total:.2f}")
    return "\\n".join(rows)
''',
     '''import unittest

from receipt import format_receipt


class ReceiptTests(unittest.TestCase):
    def test_two_lines(self):
        text = format_receipt([("pen", 2.5, 4), ("book", 10, 1)], 20)
        self.assertEqual(text, "pen x4 10.00\\nbook x1 10.00\\nTOTAL 20.00")

    def test_empty(self):
        self.assertEqual(format_receipt([], 0), "TOTAL 0.00")

    def test_rounding(self):
        self.assertEqual(format_receipt([("a", 0.333, 3)], 1), "a x3 1.00\\nTOTAL 1.00")
''', "receipt formatting")

task("search", "search", "search(items, query) returns the items (strings) that contain the query, ignoring case, in "
     "their original order; an empty query returns all items",
     '''def search(items, query):
    q = query.lower()
    return [item for item in items if q in item.lower()]
''',
     '''import unittest

from search import search


class SearchTests(unittest.TestCase):
    def test_case_insensitive(self):
        self.assertEqual(search(["Red Pen", "blue pen", "book"], "PEN"), ["Red Pen", "blue pen"])

    def test_empty_query(self):
        self.assertEqual(search(["a", "b"], ""), ["a", "b"])

    def test_no_match(self):
        self.assertEqual(search(["a"], "z"), [])
''', "item search")

task("validators", "validators", "is_valid_email(text) returns True when text has exactly one @, a non-empty part "
     "before it, and after it a domain containing a dot that is neither first nor last; otherwise False",
     '''def is_valid_email(text):
    if text.count("@") != 1:
        return False
    local, domain = text.split("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")
''',
     '''import unittest

from validators import is_valid_email


class ValidatorTests(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(is_valid_email("a@b.co"))

    def test_no_at(self):
        self.assertFalse(is_valid_email("ab.co"))

    def test_two_ats(self):
        self.assertFalse(is_valid_email("a@@b.co"))

    def test_bad_domain(self):
        self.assertFalse(is_valid_email("a@b"))
        self.assertFalse(is_valid_email("a@.co"))
        self.assertFalse(is_valid_email("a@b."))
        self.assertFalse(is_valid_email("@b.co"))
''', "email validation")

task("slugs", "slugs", "slugify(text) returns the text in lower case with every run of characters that are not "
     "letters or digits replaced by a single hyphen, and no hyphen at the start or end",
     '''import re


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
''',
     '''import unittest

from slugs import slugify


class SlugTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_punctuation_and_edges(self):
        self.assertEqual(slugify("  --Hi, there!  "), "hi-there")

    def test_digits(self):
        self.assertEqual(slugify("Top 10 items"), "top-10-items")
''', "url slugs")


def registry_line(tid):
    return f'FEATURES.append("{tid}")'


def changelog_line(tid):
    return f"- {tid}: {TASKS[tid]['changelog']}"


def stub(tid):
    t = TASKS[tid]
    return (f'"""{t["module"]}: not written yet. {t["spec"].capitalize()}."""\n\n\n'
            f"raise NotImplementedError('{t['module']} is a task on the board')\n")


def description(tid):
    t = TASKS[tid]
    return (f"In {t['module']}.py implement: {t['spec']}. Replace the NotImplementedError stub. Add "
            f"test_{t['module']}.py with at least three unittest tests for it. Then record the feature in two shared "
            f"files that other tasks also edit: append exactly the line {registry_line(tid)} to the end of registry.py, "
            f"and append exactly the line {changelog_line(tid)} to the end of CHANGELOG.md. Keep every line already "
            f"in those two files.")


def paths(tid):
    t = TASKS[tid]
    return [f"{t['module']}.py", f"test_{t['module']}.py"]
