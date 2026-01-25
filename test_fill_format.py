"""
Simple test to verify fill formatting and colors (console output only).
Run with: python test_fill_format.py
"""
from datetime import datetime


def test_fill_formatting():
    """Test fill formatting with ANSI colors"""
    print("Testing Fill Formatting with ANSI Colors\n")
    print("=" * 80)
    
    # ANSI color codes
    RESET = "\033[0m"
    BLUE = "\033[94m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    
    # Simulate some fills
    fills = [
        {"trade_id": 1, "side": "BUY", "market": "BTC-USD", "size": 0.0100, "price": 50000.00},
        {"trade_id": 2, "side": "BUY", "market": "ETH-USD", "size": 0.1500, "price": 3000.00},
        {"trade_id": 3, "side": "SELL", "market": "BTC-USD", "size": 0.0050, "price": 50500.00},
        {"trade_id": 4, "side": "SELL", "market": "SOL-USD", "size": 2.5000, "price": 125.50},
        {"trade_id": 5, "side": "BUY", "market": "ETH-USD", "size": 0.2000, "price": 2950.00},
    ]
    
    print("Colored output (alternating blue/yellow rows, BUY=green, SELL=red):")
    print("-" * 80)
    
    for fill in fills:
        trade_id = fill["trade_id"]
        side = fill["side"]
        market = fill["market"]
        size = fill["size"]
        price = fill["price"]
        
        # Determine row color
        row_color = BLUE if trade_id % 2 == 1 else YELLOW
        
        # Determine side color
        side_color = GREEN if side == "BUY" else RED
        side_padded = side.ljust(4)
        
        # Format timestamp
        ts_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        
        # Build colored line
        line = (
            f"{row_color}#{trade_id:05d} | {ts_str} | tick={trade_id:<3} | "
            f"{market:<7} | {RESET}{side_color}{side_padded}{RESET}{row_color} | "
            f"{size:.4f} @{price:.2f} | ${size*price:.2f} | "
            f"avg={price:.2f} | pos=+{size:.4f} | "
            f"rPnL=+0.00 | rPnLtot=+0.00{RESET}"
        )
        
        print(line)
    
    print("\n" + "=" * 80)
    print("Color legend:")
    print(f"  {BLUE}Odd trades (blue){RESET}")
    print(f"  {YELLOW}Even trades (yellow){RESET}")
    print(f"  {GREEN}BUY (green){RESET}")
    print(f"  {RED}SELL (red){RESET}")
    print("\nNote: BUY/SELL colors override the row color for that field only.")


if __name__ == "__main__":
    test_fill_formatting()
