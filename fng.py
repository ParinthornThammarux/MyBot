import pandas as pd
import pandas_ta as ta

s = pd.Series([1,2,3,4,5,4,3,2,1])
df = pd.DataFrame({"close": s})

macd = df.ta.macd(close="close")

print("MACD COLUMNS:", macd.columns)
print(macd.tail())
