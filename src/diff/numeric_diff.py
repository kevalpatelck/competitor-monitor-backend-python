from typing import Optional, Dict, Any

CURRENCY_SYMBOLS = {
    'INR': '₹',
    'USD': '$',
    'EUR': '€',
    'GBP': '£',
    'JPY': '¥',
    'AED': 'د.إ'
}

def diff_price(
    old_price: Optional[str],
    new_price: Optional[str],
    old_currency: Optional[str] = None,
    new_currency: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    # If either is missing, can't compare
    if old_price is None or new_price is None:
        return None

    old_symbol = CURRENCY_SYMBOLS.get(old_currency or "", old_currency or "")
    new_symbol = CURRENCY_SYMBOLS.get(new_currency or "", new_currency or "")

    try:
        old_num = float(old_price)
        new_num = float(new_price)
    except ValueError:
        # If either isn't a valid number, check if string values differ
        if old_price != new_price:
            return {
                "change_type": "price",
                "old_value": old_price,
                "new_value": new_price,
                "summary": f"Price changed from \"{old_symbol}{old_price}\" to \"{new_symbol}{new_price}\" (non-numeric values)",
            }
        return None

    # Check if currency changed
    currency_changed = old_currency and new_currency and old_currency != new_currency

    # No change in value and no change in currency
    if old_num == new_num and not currency_changed:
        return None

    # Calculate change details
    absolute_change = new_num - old_num
    percent_change = f"{((absolute_change / old_num) * 100):.2f}" if old_num != 0 else "N/A"
    direction = "increased" if absolute_change > 0 else "decreased"

    sign = "+" if absolute_change > 0 else ""
    summary = f"Price {direction} from {old_symbol}{old_num:.2f} to {new_symbol}{new_num:.2f} ({sign}{percent_change}%)"
    if currency_changed:
        summary = f"Price currency changed from {old_currency} ({old_symbol}{old_num:.2f}) to {new_currency} ({new_symbol}{new_num:.2f})"

    return {
        "change_type": "price",
        "old_value": old_price,
        "new_value": new_price,
        "summary": summary
    }

def diff_sku_count(old_count: Optional[int], new_count: Optional[int]) -> Optional[Dict[str, Any]]:
    if old_count is None or new_count is None:
        return None

    if old_count == new_count:
        return None

    difference = new_count - old_count
    direction = "increased" if difference > 0 else "decreased"
    percent_change = f"{((difference / old_count) * 100):.1f}" if old_count != 0 else "N/A"
    sign = "+" if difference > 0 else ""

    return {
        "change_type": "sku",
        "old_value": str(old_count),
        "new_value": str(new_count),
        "summary": f"Product count {direction} from {old_count} to {new_count} ({sign}{difference}, {sign}{percent_change}%)"
    }
