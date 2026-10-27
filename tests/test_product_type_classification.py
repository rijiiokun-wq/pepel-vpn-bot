from shop_bot.data_manager.database import (
    TX_TYPE_HYSTERIA,
    TX_TYPE_OLD,
    TX_TYPE_SUPER,
    TX_TYPE_VLESS,
    classify_product_type,
)


def test_classify_product_type_marks_super_aliases_as_super():
    assert classify_product_type("SUPER@") == TX_TYPE_SUPER
    assert classify_product_type("Super 2.0") == TX_TYPE_SUPER
    assert classify_product_type("Супер сервер") == TX_TYPE_SUPER


def test_classify_product_type_keeps_super_old_separate():
    assert classify_product_type("super incident host") == TX_TYPE_OLD
    assert classify_product_type("legacy super node") == TX_TYPE_OLD
    assert classify_product_type("node", "Super old plan") == TX_TYPE_OLD


def test_classify_product_type_preserves_standard_and_hysteria_types():
    assert classify_product_type("pepelmain") == TX_TYPE_VLESS
    assert classify_product_type("blitz_hysteria2") == TX_TYPE_HYSTERIA
