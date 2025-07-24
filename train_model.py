# train_model.py
import pandas as pd
import pandas_ta as ta
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
from xgboost import XGBClassifier
import joblib
import numpy as np

print("Шаг 1: Загрузка исторических данных...")
try:
    # Убедитесь, что папка 'data' существует и в ней лежит ваш файл
    df = pd.read_csv('data/sol_1m_data.csv')
except FileNotFoundError:
    print("Ошибка: Файл 'data/sol_1m_data.csv' не найден. Убедитесь, что вы создали папку 'data' и положили туда файл с историей.")
    exit()

print("Шаг 2: Расчет индикаторов (фичей)...")
df.ta.rsi(length=14, append=True)
df.ta.stoch(k=14, d=3, smooth_k=3, append=True)
df.ta.ema(length=50, append=True)
df.ta.ema(length=200, append=True)
df.dropna(inplace=True)
df.reset_index(drop=True, inplace=True)

print("Шаг 3: Разметка данных (создание цели)...")
def create_labels(df_to_label, look_forward_period=30, tp_pct=0.01, sl_pct=0.005):
    df_to_label = df_to_label.copy()
    df_to_label['future_high'] = df_to_label['high'].rolling(window=look_forward_period, min_periods=look_forward_period).max().shift(-look_forward_period)
    df_to_label['future_low'] = df_to_label['low'].rolling(window=look_forward_period, min_periods=look_forward_period).min().shift(-look_forward_period)
    
    df_to_label['tp_price'] = df_to_label['close'] * (1 + tp_pct)
    df_to_label['sl_price'] = df_to_label['close'] * (1 - sl_pct)
    
    conditions = [
        (df_to_label['future_high'] >= df_to_label['tp_price']) & (df_to_label['future_low'] > df_to_label['sl_price']),
        (df_to_label['future_high'] < df_to_label['tp_price']) & (df_to_label['future_low'] <= df_to_label['sl_price']),
        (df_to_label['future_high'] >= df_to_label['tp_price']) & (df_to_label['future_low'] <= df_to_label['sl_price'])
    ]
    choices = [1, 0, np.nan] # 1=TP, 0=SL, nan=неопределенно
    df_to_label['target'] = np.select(conditions, choices, default=np.nan)
    
    df_to_label.dropna(subset=['target'], inplace=True)
    df_to_label['target'] = df_to_label['target'].astype(int)
    return df_to_label

labeled_df = create_labels(df)
print(f"Соотношение классов после разметки:\n{labeled_df['target'].value_counts(normalize=True)}")

print("Шаг 4: Обучение ML модели (XGBoost)...")
features = ['RSI_14', 'STOCHk_14_3_3', 'EMA_50', 'EMA_200', 'close', 'volume']
X = labeled_df[features]
y = labeled_df['target']

X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

model = XGBClassifier(use_label_encoder=False, eval_metric='logloss', n_estimators=100, learning_rate=0.1)
model.fit(X_train, y_train)

print("Шаг 5: Оценка качества модели...")
y_pred = model.predict(X_test)
print(classification_report(y_test, y_pred))

print("Шаг 6: Сохранение модели...")
joblib.dump(model, 'trading_model.pkl')
print("\n✅ Готово! Модель обучена и сохранена в файл 'trading_model.pkl'.")
