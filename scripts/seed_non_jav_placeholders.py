"""Attach curated non-JAV actors and real public portraits only (no placeholders)."""

from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from actor_avatars import (
    detect_image_ext,
    fetch_real_portrait,
    image_filename,
    is_designed_identicon,
    is_solid_placeholder,
    notes_indicate_placeholder,
    notes_indicate_real_photo,
    theporndb_token_from_env,
)
from actor_avatars import _looks_searchable_person_name


CATALOG_PATH = ROOT / "data" / "non-jav-actors.json"
IMAGE_DIR = ROOT / "data" / "actor-images"

EXTRA_ACTORS: tuple[dict[str, object], ...] = (
    {
        "name": "신재은",
        "aliases": ["Shin Jae-eun", "Shin Jae Eun"],
        "groups": ["korean"],
        "categories": ["Korea"],
        "biography": "Korean adult performer; aliases used for directory matching.",
    },
    {
        "name": "감동란",
        "aliases": ["Gam Dong-ran", "Gam Dongran"],
        "groups": ["korean"],
        "categories": ["Korea"],
        "biography": "Korean adult performer.",
    },
    {
        "name": "이채담",
        "aliases": ["Lee Chae-dam", "Lee Chaedam"],
        "groups": ["korean"],
        "categories": ["Korea"],
    },
    {
        "name": "서아린",
        "aliases": ["Seo A-rin", "Seo Arin"],
        "groups": ["korean"],
        "categories": ["Korea"],
    },
    {
        "name": "김세희",
        "aliases": ["Kim Se-hee", "Kim Sehee"],
        "groups": ["korean"],
        "categories": ["Korea"],
    },
    {
        "name": "박하윤",
        "aliases": ["Park Ha-yoon", "Park Hayoon"],
        "groups": ["korean"],
        "categories": ["Korea"],
    },
    {
        "name": "윤설아",
        "aliases": ["Yoon Seol-ah", "Yoon Seolah"],
        "groups": ["korean"],
        "categories": ["Korea"],
    },
    {
        "name": "정다은",
        "aliases": ["Jung Da-eun", "Jung Daeun"],
        "groups": ["korean"],
        "categories": ["Korea"],
    },
    {
        "name": "최시아",
        "aliases": ["Choi Si-a", "Choi Sia"],
        "groups": ["korean"],
        "categories": ["Korea"],
    },
    {
        "name": "유하나",
        "aliases": ["Yu Ha-na", "Yoo Hana"],
        "groups": ["korean"],
        "categories": ["Korea"],
    },
    {
        "name": "오지혜",
        "aliases": ["Oh Ji-hye", "Oh Jihye"],
        "groups": ["korean"],
        "categories": ["Korea"],
    },
    {
        "name": "한소라",
        "aliases": ["Han So-ra", "Han Sora"],
        "groups": ["korean"],
        "categories": ["Korea"],
    },
    {
        "name": "李蓉蓉",
        "aliases": ["Li Rongrong"],
        "groups": ["madou"],
        "categories": ["China"],
    },
    {
        "name": "林思妤",
        "aliases": ["Lin Siyu"],
        "groups": ["madou"],
        "categories": ["China"],
    },
    {
        "name": "凌薇",
        "aliases": ["Ling Wei"],
        "groups": ["madou"],
        "categories": ["China"],
    },
    {
        "name": "韩棠",
        "aliases": ["Han Tang"],
        "groups": ["madou"],
        "categories": ["China"],
    },
    {
        "name": "季妍曦",
        "aliases": ["Ji Yanxi"],
        "groups": ["madou"],
        "categories": ["China"],
    },
    {
        "name": "徐蕾",
        "aliases": ["Xu Lei"],
        "groups": ["madou"],
        "categories": ["China"],
    },
    {
        "name": "宋南伊",
        "aliases": ["Song Nanyi"],
        "groups": ["madou"],
        "categories": ["China"],
    },
    {
        "name": "袁子仪",
        "aliases": ["Yuan Ziyi"],
        "groups": ["madou"],
        "categories": ["China"],
    },
    {
        "name": "苏小暖",
        "aliases": ["Su Xiaonuan"],
        "groups": ["madou"],
        "categories": ["China"],
    },
    {
        "name": "沈樵",
        "aliases": ["Shen Qiao"],
        "groups": ["madou"],
        "categories": ["China"],
    },
    {
        "name": "白茹",
        "aliases": ["Bai Ru"],
        "groups": ["madou"],
        "categories": ["China"],
    },
    {
        "name": "唐茜",
        "aliases": ["Tang Qian"],
        "groups": ["madou"],
        "categories": ["China"],
    },
    {
        "name": "Adriana Chechik",
        "aliases": ["Adriana Chechick"],
        "groups": ["western"],
        "categories": ["Europe"],
    },
    {
        "name": "Elsa Jean",
        "aliases": [],
        "groups": ["western"],
        "categories": ["Europe"],
    },
    {
        "name": "Kendra Lust",
        "aliases": [],
        "groups": ["western"],
        "categories": ["Europe"],
    },
    {
        "name": "Lisa Ann",
        "aliases": [],
        "groups": ["western"],
        "categories": ["Europe"],
    },
    {
        "name": "Nicole Aniston",
        "aliases": [],
        "groups": ["western"],
        "categories": ["Europe"],
    },
    {
        "name": "Eva Elfie",
        "aliases": [],
        "groups": ["western", "onlyfans"],
        "categories": ["Europe", "Other"],
    },
    {
        "name": "Little Caprice",
        "aliases": ["Caprice"],
        "groups": ["western"],
        "categories": ["Europe"],
    },
    {
        "name": "Gianna Dior",
        "aliases": [],
        "groups": ["western"],
        "categories": ["Europe"],
    },
    {
        "name": "Autumn Falls",
        "aliases": [],
        "groups": ["western"],
        "categories": ["Europe"],
    },
    {
        "name": "Abigail Mac",
        "aliases": [],
        "groups": ["western"],
        "categories": ["Europe"],
    },
    {
        "name": "Alina Lopez",
        "aliases": [],
        "groups": ["western"],
        "categories": ["Europe"],
    },
    {
        "name": "Tori Black",
        "aliases": [],
        "groups": ["western"],
        "categories": ["Europe"],
    },
    {
        "name": "Asa Akira",
        "aliases": [],
        "groups": ["western"],
        "categories": ["Europe"],
    },
    {
        "name": "Emily Willis",
        "aliases": [],
        "groups": ["western"],
        "categories": ["Europe"],
    },
)

CHINA_EXTRA: tuple[dict[str, object], ...] = (
    {"name": '麻豆传媒', "aliases": ['Madou', 'Madou Media', '麻豆', '麻豆传媒映画', 'MD'], "groups": ['madou', 'studio'], "categories": ["China"], "biography": '国产成人工作室/创作者条目，用于目录匹配。'},
    {"name": '果冻传媒', "aliases": ['Jelly Media', '果冻', 'Jelly Media 果冻传媒'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人工作室果冻传媒。'},
    {"name": '天美传媒', "aliases": ['Tianmei', '天美', 'TM传媒'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人工作室天美传媒。'},
    {"name": '星空无限传媒', "aliases": ['Xingkong', '星空传媒', '星空无限', 'XK传媒'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人工作室星空无限传媒。'},
    {"name": '91制片厂', "aliases": ['91 Studio', '91厂', '九一制片厂'], "groups": ['madou', '91-studio'], "categories": ["China"], "biography": '国产成人工作室91制片厂。'},
    {"name": '精东影业', "aliases": ['Jingdong', '精东', '精东传媒'], "groups": ['madou', 'jingdong'], "categories": ["China"], "biography": '国产成人工作室精东影业。'},
    {"name": '蜜桃影像', "aliases": ['Mitao', '蜜桃传媒', '蜜桃影像传媒'], "groups": ['madou', 'mitao'], "categories": ["China"], "biography": '国产成人工作室蜜桃影像。'},
    {"name": '皇家华人', "aliases": ['Royal Chinese', '皇家华人传媒'], "groups": ['madou', 'royal'], "categories": ["China"], "biography": '国产成人工作室皇家华人。'},
    {"name": '糖心Vlog', "aliases": ['糖心', 'Sugarheart', 'TxVlog', '糖心vlog'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国产成人短视频/博主向创作者品牌。'},
    {"name": '起点传媒', "aliases": ['Qidian Media', '起点'], "groups": ['madou', 'studio'], "categories": ["China"], "biography": '国产成人工作室起点传媒。'},
    {"name": '大象传媒', "aliases": ['Elephant Media', '大象'], "groups": ['madou', 'studio'], "categories": ["China"], "biography": '国产成人工作室大象传媒。'},
    {"name": '乐播传媒', "aliases": ['Lebo', '乐播'], "groups": ['madou', 'studio'], "categories": ["China"], "biography": '国产成人工作室乐播传媒。'},
    {"name": '杏吧', "aliases": ['Xingba', '性吧', '杏吧论坛'], "groups": ['xingba', 'x-xingba'], "categories": ["China"], "biography": '杏吧/性吧相关创作者聚合名。'},
    {"name": '李文雯', "aliases": ['Li Wenwen', '李文文', 'Wenwen Li'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '沈芯语', "aliases": ['Shen Xinyu', '沈心语', 'Xinyu Shen'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '苏清歌', "aliases": ['Su Qingge', 'Qingge Su'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '姚宛儿', "aliases": ['Yao Waner', '姚婉儿', 'Waner Yao'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '温芮欣', "aliases": ['Wen Ruixin', 'Ruixin Wen'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '李娜娜', "aliases": ['Li Nana', 'Nana Li', '李娜'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '张芸熙', "aliases": ['Zhang Yunxi', 'Yunxi Zhang'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '王晓禹', "aliases": ['Wang Xiaoyu', 'Xiaoyu Wang'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '李文静', "aliases": ['Li Wenjing', 'Wenjing Li'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '赵晓涵', "aliases": ['Zhao Xiaohan', 'Xiaohan Zhao'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '韩小妮', "aliases": ['Han Xiaoni', 'Xiaoni Han'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '吴芳宜', "aliases": ['Wu Fangyi', 'Fangyi Wu'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '林凤娇', "aliases": ['Lin Fengjiao', 'Fengjiao Lin'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '梁芸菲', "aliases": ['Liang Yunfei', 'Yunfei Liang'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '宋东琳', "aliases": ['Song Donglin', 'Donglin Song'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '周宁', "aliases": ['Zhou Ning', 'Ning Zhou'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '夏禹乔', "aliases": ['Xia Yuqiao', 'Yuqiao Xia'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '苏语棠', "aliases": ['Su Yutang', 'Yutang Su'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '沈佳妮', "aliases": ['Shen Jiani', 'Jiani Shen'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '唐雨辰', "aliases": ['Tang Yuchen', 'Yuchen Tang'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '顾小北', "aliases": ['Gu Xiaobei', 'Xiaobei Gu'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '李文瑞', "aliases": ['Li Wenrui', 'Wenrui Li'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '张雅婷', "aliases": ['Zhang Yating', 'Yating Zhang', '张雅庭'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '王思琪', "aliases": ['Wang Siqi', 'Siqi Wang'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '吴倩倩', "aliases": ['Wu Qianqian', 'Qianqian Wu'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '赵金金', "aliases": ['Zhao Jinjin', 'Jinjin Zhao'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '李文茜', "aliases": ['Li Wenqian', 'Wenqian Li'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '苏小小', "aliases": ['Su Xiaoxiao', 'Xiaoxiao Su'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '仙儿媛', "aliases": ['Xian Eryuan', 'Eryuan', '仙儿'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '米苏', "aliases": ['Mi Su', 'Misu', '米苏酱'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '苡若', "aliases": ['Yi Ruo', 'Yiruo'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '苡琍', "aliases": ['Yi Li', 'Yili', '苡莉'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '苡沫', "aliases": ['Yi Mo', 'Yimo'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '苡婷', "aliases": ['Yi Ting', 'Yiting'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '苏娅', "aliases": ['Su Ya', 'Suya'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '苏婉', "aliases": ['Su Wan', 'Suwan'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '苏沫', "aliases": ['Su Mo', 'Sumo Su'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '李允熙', "aliases": ['Li Yunxi', 'Yunxi Li'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '李梦瑶', "aliases": ['Li Mengyao', 'Mengyao Li'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '王婉悠', "aliases": ['Wang Wanyou', 'Wanyou Wang'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '王紫琪', "aliases": ['Wang Ziqi', 'Ziqi Wang'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '张恩慈', "aliases": ['Zhang Enci', 'Enci Zhang'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '张筱雨', "aliases": ['Zhang Xiaoyu', 'Xiaoyu Zhang', '张小雨'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '陈小花', "aliases": ['Chen Xiaohua', 'Xiaohua Chen'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '陈静怡', "aliases": ['Chen Jingyi', 'Jingyi Chen'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '刘思怡', "aliases": ['Liu Siyi', 'Siyi Liu'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '刘晓燕', "aliases": ['Liu Xiaoyan', 'Xiaoyan Liu'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '黄晶晶', "aliases": ['Huang Jingjing', 'Jingjing Huang'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '黄晓彤', "aliases": ['Huang Xiaotong', 'Xiaotong Huang'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '周妍希', "aliases": ['Zhou Yanxi', 'Yanxi Zhou'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '周小婉', "aliases": ['Zhou Xiaowan', 'Xiaowan Zhou'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '白晶晶', "aliases": ['Bai Jingjing', 'Jingjing Bai'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '陈美惠', "aliases": ['Chen Meihui', 'Meihui Chen'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '雪千寻', "aliases": ['Xue Qianxun', 'Qianxun Xue'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '韩小冉', "aliases": ['Han Xiaoran', 'Xiaoran Han'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '李文文', "aliases": ['Li Wenwen MD', 'Wenwen'], "groups": ['madou', '91-studio'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '白虎冰冰', "aliases": ['Baihu Bingbing', 'Bingbing Baihu'], "groups": ['madou', '91-studio'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '软萌白虎', "aliases": ['Ruanmeng Baihu'], "groups": ['madou', '91-studio'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '吴芳妮', "aliases": ['Wu Fangni', 'Fangni Wu'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '李燕燕', "aliases": ['Li Yanyan', 'Yanyan Li'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '王晓晓', "aliases": ['Wang Xiaoxiao', 'Xiaoxiao Wang'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '张芸芸', "aliases": ['Zhang Yunyun', 'Yunyun Zhang'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '林晓雪', "aliases": ['Lin Xiaoxue', 'Xiaoxue Lin'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '王语嫣', "aliases": ['Wang Yuyan', 'Yuyan Wang'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '许雅妍', "aliases": ['Xu Yayan', 'Yayan Xu'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '赵婉清', "aliases": ['Zhao Wanqing', 'Wanqing Zhao'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '孙怡然', "aliases": ['Sun Yiran', 'Yiran Sun'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '何美琳', "aliases": ['He Meilin', 'Meilin He'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '高雅楠', "aliases": ['Gao Yanan', 'Yanan Gao'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '冯诗诗', "aliases": ['Feng Shishi', 'Shishi Feng'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '邓小柔', "aliases": ['Deng Xiaorou', 'Xiaorou Deng'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '曹婉儿', "aliases": ['Cao Waner', 'Waner Cao'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '潘金妮', "aliases": ['Pan Jinni', 'Jinni Pan'], "groups": ['madou', '91-studio'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '蒋雨萱', "aliases": ['Jiang Yuxuan', 'Yuxuan Jiang'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '魏思琪', "aliases": ['Wei Siqi', 'Siqi Wei'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '卢婉婷', "aliases": ['Lu Wanting', 'Wanting Lu'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '方芷涵', "aliases": ['Fang Zhihan', 'Zhihan Fang'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '田晓慧', "aliases": ['Tian Xiaohui', 'Xiaohui Tian'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '任梦瑶', "aliases": ['Ren Mengyao', 'Mengyao Ren'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '贾小曼', "aliases": ['Jia Xiaoman', 'Xiaoman Jia'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '夏婉婉', "aliases": ['Xia Wanwan', 'Wanwan Xia'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '唐小曼', "aliases": ['Tang Xiaoman', 'Xiaoman Tang'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '宋婉清', "aliases": ['Song Wanqing', 'Wanqing Song'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '韩雨菲', "aliases": ['Han Yufei', 'Yufei Han'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '林可儿', "aliases": ['Lin Keer', 'Keer Lin'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '程晓诺', "aliases": ['Cheng Xiaonuo', 'Xiaonuo Cheng'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '叶思思', "aliases": ['Ye Sisi', 'Sisi Ye'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '罗小七', "aliases": ['Luo Xiaoqi', 'Xiaoqi Luo'], "groups": ['madou', '91-studio'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '金薇薇', "aliases": ['Jin Weiwei', 'Weiwei Jin'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '倪梦琪', "aliases": ['Ni Mengqi', 'Mengqi Ni'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '齐晓萱', "aliases": ['Qi Xiaoxuan', 'Xiaoxuan Qi'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '孟晓彤', "aliases": ['Meng Xiaotong', 'Xiaotong Meng'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '戴婉儿', "aliases": ['Dai Waner', 'Waner Dai'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '侯雨桐', "aliases": ['Hou Yutong', 'Yutong Hou'], "groups": ['madou', 'xingkong'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '龚小雅', "aliases": ['Gong Xiaoya', 'Xiaoya Gong'], "groups": ['madou'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '江语彤', "aliases": ['Jiang Yutong', 'Yutong Jiang'], "groups": ['madou', 'tianmei'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '钟晓薇', "aliases": ['Zhong Xiaowei', 'Xiaowei Zhong'], "groups": ['madou', 'jelly'], "categories": ["China"], "biography": '国产成人影像演员（麻豆/果冻/天美/星空等厂牌长尾）。'},
    {"name": '大神探花', "aliases": ['Dashen Tanhua', '探花大神号'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '探花郎', "aliases": ['Tanhua Lang', 'Tanghua Lang'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '韦小宝探花', "aliases": ['Wei Xiaobao Tanhua', '韦小宝'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '西门探花', "aliases": ['Ximen Tanhua', '西门庆探花'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '金先生探花', "aliases": ['Jin Xiansheng', '金先生'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '老王探花', "aliases": ['Laowang Tanhua', '王探花'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '北京探花', "aliases": ['Beijing Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '上海探花', "aliases": ['Shanghai Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '杭州探花', "aliases": ['Hangzhou Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '成都探花', "aliases": ['Chengdu Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '深圳探花', "aliases": ['Shenzhen Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '广州探花', "aliases": ['Guangzhou Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '南京探花', "aliases": ['Nanjing Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '武汉探花', "aliases": ['Wuhan Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '重庆探花', "aliases": ['Chongqing Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '西安探花', "aliases": ['Xian Tanhua', '西安探花系列'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '天津探花', "aliases": ['Tianjin Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '苏州探花', "aliases": ['Suzhou Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '长沙探花', "aliases": ['Changsha Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '青岛探花', "aliases": ['Qingdao Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '厦门探花', "aliases": ['Xiamen Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '昆明探花', "aliases": ['Kunming Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '郑州探花', "aliases": ['Zhengzhou Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '东莞探花', "aliases": ['Dongguan Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '佛山探花', "aliases": ['Foshan Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '珠海探花', "aliases": ['Zhuhai Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '宁波探花', "aliases": ['Ningbo Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '温州探花', "aliases": ['Wenzhou Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '无锡探花', "aliases": ['Wuxi Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '大连探花', "aliases": ['Dalian Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '沈阳探花', "aliases": ['Shenyang Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '哈尔滨探花', "aliases": ['Harbin Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '三亚探花', "aliases": ['Sanya Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '南宁探花', "aliases": ['Nanning Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '台湾探花', "aliases": ['Taiwan Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '香港探花', "aliases": ['Hong Kong Tanhua', 'HK探花'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '夜色探花', "aliases": ['Yeshe Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '酒店探花', "aliases": ['Hotel Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '外围探花', "aliases": ['Waiwei Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '探花君', "aliases": ['Tanhua Jun'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '探花达人', "aliases": ['Tanhua Daren'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '探花小哥', "aliases": ['Tanhua Xiaoge'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '探花阿强', "aliases": ['Tanhua Aqiang', '阿强探花'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '探花阿伟', "aliases": ['Tanhua Awei', '阿伟探花'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '探花老司机', "aliases": ['Tanhua Laosiji'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '真实探花', "aliases": ['Zhenshi Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '高端探花', "aliases": ['Highend Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '模特探花', "aliases": ['Model Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '少妇探花', "aliases": ['Shaofu Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '人妻探花', "aliases": ['Renqi Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '护士探花', "aliases": ['Nurse Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '健身探花', "aliases": ['Fitness Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '纹身探花', "aliases": ['Tattoo Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '网红探花', "aliases": ['Wanghong Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '直播探花', "aliases": ['Zhibo Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '探花兄弟', "aliases": ['Tanhua Brothers'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '双飞探花', "aliases": ['Shuangfei Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '探花日记', "aliases": ['Tanhua Diary'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '探花实录', "aliases": ['Tanhua Shilu'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '午夜探花', "aliases": ['Wuye Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '深夜探花', "aliases": ['Shenyie Tanhua', '深夜探花系列'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '出差探花', "aliases": ['Chuchai Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '商务探花', "aliases": ['Shangwu Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": 'KTV探花', "aliases": ['KTV Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '会所探花', "aliases": ['Huisuo Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '桑拿探花', "aliases": ['Sauna Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '足疗探花', "aliases": ['Zuliao Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '按摩探花', "aliases": ['Anmo Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": 'SPA探花', "aliases": ['Spa Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '包养探花', "aliases": ['Baoyang Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '金主探花', "aliases": ['Jinzhu Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '海王探花', "aliases": ['Haiwang Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '黑牛探花', "aliases": ['Heiniu Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '白马探花', "aliases": ['Baima Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '眼镜探花', "aliases": ['Yanjing Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '西装探花', "aliases": ['Xizhuang Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '肌肉男探花', "aliases": ['Jirou Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '胖哥探花', "aliases": ['Pangge Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '瘦子探花', "aliases": ['Shouzi Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '东北探花', "aliases": ['Dongbei Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '河南探花', "aliases": ['Henan Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '山东探花', "aliases": ['Shandong Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '安徽探花', "aliases": ['Anhui Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '江西探花', "aliases": ['Jiangxi Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '福建探花', "aliases": ['Fujian Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '广西探花', "aliases": ['Guangxi Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '海南探花', "aliases": ['Hainan Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '云南探花', "aliases": ['Yunnan Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '贵州探花', "aliases": ['Guizhou Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '义乌探花', "aliases": ['Yiwu Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '洛阳探花', "aliases": ['Luoyang Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '常州探花', "aliases": ['Changzhou Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '嘉兴探花', "aliases": ['Jiaxing Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '台州探花', "aliases": ['Taizhou Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '金华探花', "aliases": ['Jinhua Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '中山探花', "aliases": ['Zhongshan Tanhua'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '91大神', "aliases": ['91 Dashen', '九一大神'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '91小宝', "aliases": ['91 Xiaobao'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '91小严', "aliases": ['91 Xiaoyan'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '91瓜弟', "aliases": ['91 Guadi'], "groups": ['tanhua', '91-tanhua'], "categories": ["China"], "biography": '国产探花系列创作者/系列名，用于目录与文件名匹配。'},
    {"name": '约炮王', "aliases": ['Yuepao Wang'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '约炮达人', "aliases": ['Yuepao Daren'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '约炮小哥', "aliases": ['Yuepao Xiaoge'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '约炮日记', "aliases": ['Yuepao Diary'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '约炮实录', "aliases": ['Yuepao Shilu'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '约炮酒店', "aliases": ['Yuepao Hotel'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '约炮模特', "aliases": ['Yuepao Model'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '约炮空姐', "aliases": ['Yuepao Kongjie'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '约炮护士', "aliases": ['Yuepao Nurse'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '约炮少妇', "aliases": ['Yuepao Shaofu'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '约炮人妻', "aliases": ['Yuepao Renqi'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '约炮外围', "aliases": ['Yuepao Waiwei'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '约炮网红', "aliases": ['Yuepao Wanghong'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '约炮兼职', "aliases": ['Yuepao Jianzhi'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '约炮双胞胎', "aliases": ['Yuepao Twins'], "groups": ['yuepao', 'x-tanhua'], "categories": ["China"], "biography": '国产约炮系列创作者，探花/约炮长尾。'},
    {"name": '探店小雪', "aliases": ['Xiaoxue Tandian', '小雪'], "groups": ['tandian', 'x-tanhua'], "categories": ["China"], "biography": '国产探店系列创作者。'},
    {"name": '探店小月', "aliases": ['Xiaoyue Tandian', '小月'], "groups": ['tandian', 'x-tanhua'], "categories": ["China"], "biography": '国产探店系列创作者。'},
    {"name": '探店小慧', "aliases": ['Xiaohui Tandian', '小慧'], "groups": ['tandian', 'x-tanhua'], "categories": ["China"], "biography": '国产探店系列创作者。'},
    {"name": '探店小琳', "aliases": ['Xiaolin Tandian', '小琳'], "groups": ['tandian', 'x-tanhua'], "categories": ["China"], "biography": '国产探店系列创作者。'},
    {"name": '探店小彤', "aliases": ['Xiaotong Tandian', '小彤'], "groups": ['tandian', 'x-tanhua'], "categories": ["China"], "biography": '国产探店系列创作者。'},
    {"name": '探店小薇', "aliases": ['Xiaowei Tandian', '小薇'], "groups": ['tandian', 'x-tanhua'], "categories": ["China"], "biography": '国产探店系列创作者。'},
    {"name": '探店小燕', "aliases": ['Xiaoyan Tandian Shop', '小燕探店'], "groups": ['tandian', 'x-tanhua'], "categories": ["China"], "biography": '国产探店系列创作者。'},
    {"name": '探店小琪', "aliases": ['Xiaoqi Tandian', '小琪'], "groups": ['tandian', 'x-tanhua'], "categories": ["China"], "biography": '国产探店系列创作者。'},
    {"name": '小恩雅', "aliases": ['Xiao Enya', 'Enya', '恩雅博主'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '软软趴', "aliases": ['Ruanruan Pa', '软软'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '小小酥', "aliases": ['Xiaoxiaosu', '小酥'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '酥酥酱', "aliases": ['Susu Jiang', '酥酥'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '奶瓶精', "aliases": ['Naipingjing', '奶瓶'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '娜美酱', "aliases": ['Namei Jiang', '娜美博主'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '玉足女王', "aliases": ['Yuzu Queen', '玉足'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '丝足福利姬', "aliases": ['Sizu Fuli', '丝足博主'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '林襄', "aliases": ['Lin Xiang', '乐天女孩林襄', 'Xiang Lin'], "groups": ['swag', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '雪碧SWAG', "aliases": ['Sprite SWAG', 'SWAG雪碧', 'Xuebi'], "groups": ['swag', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '婕咪', "aliases": ['Jamie SWAG', 'Jiemi', '婕咪SWAG'], "groups": ['swag', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '陈香菱', "aliases": ['Chen Xiangling', 'Xiangling Chen'], "groups": ['swag', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '璇璇SWAG', "aliases": ['Xuanxuan SWAG', '璇璇'], "groups": ['swag', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '小夜夜', "aliases": ['Xiaoyeye', '夜夜博主'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '辣椒酱酱', "aliases": ['Lajiaojiang', '小辣椒酱'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '粉色情人', "aliases": ['Pink Lover', '粉色情人博主'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '粉色小猪', "aliases": ['Pink Piggy'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '桃桃酱', "aliases": ['Taotao Jiang', '桃桃博主'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '蜜桃酱', "aliases": ['Mitao Jiang', '蜜桃博主'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '樱桃酱', "aliases": ['Yingtao Jiang'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '小水水', "aliases": ['Xiaoshuishui', '水水博主'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '小鱼儿博主', "aliases": ['Xiaoyuer', '鱼儿'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '鱼鱼酱', "aliases": ['Yuyu Jiang'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '猫猫酱', "aliases": ['Maomao Jiang', '猫酱'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '兔兔酱', "aliases": ['Tutu Jiang', '兔酱'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '安乔乔', "aliases": ['An Qiaoqiao', '乔乔博主'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '乔安酱', "aliases": ['Qiaoan Jiang', '乔安'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '艾米酱', "aliases": ['Aimi Jiang', '艾米博主'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '米米酱', "aliases": ['Mimi Jiang'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '糖糖酱', "aliases": ['Tangtang Jiang', '糖糖博主'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '可可酱', "aliases": ['Keke Jiang', '可可博主'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '露露酱', "aliases": ['Lulu Jiang', '露露博主'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '晚晚酱', "aliases": ['Wanwan Jiang'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '香香酱', "aliases": ['Xiangxiang Jiang'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '妍妍酱', "aliases": ['Yanyan Jiang'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '奶茶酱', "aliases": ['Naicha Jiang'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '琪琪酱', "aliases": ['Qiqi Jiang'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '小美酱', "aliases": ['Xiaomei Jiang'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '小倩酱', "aliases": ['Xiaoqian Jiang'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '桃子酱', "aliases": ['Taozi Jiang'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '樱花酱', "aliases": ['Yinghua Jiang'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '熙爱酱', "aliases": ['Xiai Jiang'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '反差博主', "aliases": ['Fancha Blogger', '推特反差'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '推特福利姬', "aliases": ['Twitter Fuli', '福利姬博主'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": 'OnlyFans华语', "aliases": ['OF Huayu', '华语OF'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '推特华语博主', "aliases": ['Twitter CN', '华语推特'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '海角社区', "aliases": ['Haijiao', '海角'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '完具', "aliases": ['Wanju', '完具酱'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '小青茗', "aliases": ['Xiao Qingming', '青茗'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '桃井睿', "aliases": ['Taojing Rui'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '香草少女M', "aliases": ['Vanilla Girl M', '香草M'], "groups": ['onlyfans', 'twitter', 'blogger'], "categories": ["China"], "biography": '国内成人博主/创作者（OnlyFans/推特/SWAG 风格），用于目录匹配。'},
    {"name": '杏吧小妻', "aliases": ['Xingba Xiaoqi'], "groups": ['xingba', 'x-xingba'], "categories": ["China"], "biography": '杏吧/性吧长尾创作者名。'},
    {"name": '杏吧少妇', "aliases": ['Xingba Shaofu'], "groups": ['xingba', 'x-xingba'], "categories": ["China"], "biography": '杏吧/性吧长尾创作者名。'},
    {"name": '杏吧人妻', "aliases": ['Xingba Renqi'], "groups": ['xingba', 'x-xingba'], "categories": ["China"], "biography": '杏吧/性吧长尾创作者名。'},
    {"name": '杏吧模特', "aliases": ['Xingba Model'], "groups": ['xingba', 'x-xingba'], "categories": ["China"], "biography": '杏吧/性吧长尾创作者名。'},
    {"name": '杏吧外围', "aliases": ['Xingba Waiwei'], "groups": ['xingba', 'x-xingba'], "categories": ["China"], "biography": '杏吧/性吧长尾创作者名。'},
    {"name": '杏吧网红', "aliases": ['Xingba Wanghong'], "groups": ['xingba', 'x-xingba'], "categories": ["China"], "biography": '杏吧/性吧长尾创作者名。'},
)

EXTRA_ACTORS = EXTRA_ACTORS + CHINA_EXTRA


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _safe_match_names(name: str, aliases: list[str]) -> list[str]:
    names: list[str] = []
    for value in [name, *aliases]:
        cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
        key = _normalize(cleaned).lstrip("@")
        if not key:
            continue
        if key.isascii() and len("".join(ch for ch in key if ch.isalnum())) < 4:
            continue
        if not key.isascii() and len(key) < 2:
            continue
        if cleaned not in names:
            names.append(cleaned)
    return names



def _clear_actor_images(image_dir: Path, name: str) -> int:
    removed = 0
    stem = image_filename(name, ".png").rsplit(".", 1)[0]
    for leftover in image_dir.glob(f"{stem}.*"):
        leftover.unlink(missing_ok=True)
        removed += 1
    return removed


def _should_keep_existing(path: Path, notes: str | None) -> bool:
    if not path.is_file():
        return False
    if notes_indicate_placeholder(notes):
        return False
    if is_solid_placeholder(path) or is_designed_identicon(path):
        return False
    if notes_indicate_real_photo(notes):
        return True
    if path.suffix.lower() in {".jpg", ".jpeg", ".webp", ".gif"}:
        return path.stat().st_size >= 2000
    return path.stat().st_size >= 20_000 and not is_designed_identicon(path)


def main() -> None:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8-sig"))
    actors: list[dict[str, object]] = list(catalog["actors"])
    existing = {_normalize(str(actor["name"])) for actor in actors}

    extra_alias_map = {
        "lana rhodes": ["Lana Rhoades"],
        "janice griffith": ["Janice Griffith"],
        "mia malkova": ["Mia Malkova"],
        "angela white": ["Angela White"],
        "riley reid": ["Riley Reid"],
        "hongkongdoll": ["HKDoll", "Hong Kong Doll", "玩偶姐姐", "HKDoll姐姐"],
        "吴梦梦": ["Wu Mengmeng"],
        "夏晴子": ["Xia Qingzi"],
        "苏畅": ["Su Chang"],
        "林予曦": ["Lin Yuxi"],
        "沈娜娜": ["Shen Nana"],
        "艾秋": ["Ai Qiu"],
        "艾鲤": ["Ai Li"],
        "孟若羽": ["Meng Ruoyu"],
        "楚梦舒": ["Chu Mengshu"],
        "江南第一深情": ["Jiangnan"],
        "麻豆传媒": ["Madou Media", "麻豆映画"],
        "果冻传媒": ["JellyMedia"],
        "天美传媒": ["Tianmei Media"],
        "星空无限传媒": ["Xingkong Media"],
        "91制片厂": ["91 Films"],
        "糖心vlog": ["糖心vlog", "Sugar Heart"],
        "林襄": ["Lin Hsiang"],
        "大神探花": ["探花大神"],
        "韦小宝探花": ["探花韦小宝"],
        "金先生探花": ["探花金先生"],
    }

    added = 0
    for extra in EXTRA_ACTORS:
        if _normalize(str(extra["name"])) in existing:
            continue
        aliases = list(extra.get("aliases", []))  # type: ignore[arg-type]
        name = str(extra["name"])
        actor = {
            "name": name,
            "aliases": aliases,
            "groups": list(extra["groups"]),  # type: ignore[arg-type]
            "categories": list(extra["categories"]),  # type: ignore[arg-type]
            "match_names": _safe_match_names(name, aliases),
            "image_file": None,
            "biography": extra.get("biography") or f"Curated {extra['categories'][0]} adult performer seed.",
            "notes": "Awaiting real portrait; upload via actor library or re-run portrait seed.",
        }
        actors.append(actor)
        existing.add(_normalize(name))
        added += 1

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    token = theporndb_token_from_env(ROOT / ".env")
    real_photos = 0
    kept_existing = 0
    without_image = 0
    deleted_placeholders = 0

    for actor in actors:
        name = str(actor["name"])
        extra_aliases = extra_alias_map.get(_normalize(name), [])
        aliases = list(dict.fromkeys([*actor.get("aliases", []), *extra_aliases]))  # type: ignore[arg-type]
        actor["aliases"] = aliases
        actor["match_names"] = _safe_match_names(name, aliases)
        notes = str(actor.get("notes") or "") if actor.get("notes") is not None else None

        current_name = actor.get("image_file")
        current_path = IMAGE_DIR / str(current_name) if current_name else None
        if current_path is not None and _should_keep_existing(current_path, notes):
            actor["image_file"] = current_path.name
            if not notes_indicate_real_photo(notes):
                actor["notes"] = "Existing local portrait kept (non-placeholder)."
            kept_existing += 1
            real_photos += 1
        else:
            groups = {str(item).casefold() for item in (actor.get("groups") or [])}
            skip_groups = {
                "studio",
                "tanhua",
                "91-tanhua",
                "yuepao",
                "x-tanhua",
                "tandian",
                "xingba",
                "x-xingba",
            }
            # Public EN Wikipedia / Wikidata coverage is mainly Western stage names.
            # Romanized Chinese/Korean aliases rarely have usable Commons portraits.
            def _latin_primary(value: str) -> bool:
                letters = sum(ch.isalpha() and ch.isascii() for ch in value)
                visible = sum(not ch.isspace() for ch in value)
                return letters >= 4 and letters >= max(1, visible // 2)

            searchable = (
                not (groups & skip_groups)
                and ("western" in groups or _latin_primary(name))
                and any(_looks_searchable_person_name(value) for value in [name, *aliases])
            )
            fetched = (
                fetch_real_portrait(name, aliases, theporndb_token=token) if searchable else None
            )
            if fetched is not None:
                content, source_note = fetched
                ext = detect_image_ext(content) or ".jpg"
                filename = image_filename(name, ext)
                deleted_placeholders += _clear_actor_images(IMAGE_DIR, name)
                (IMAGE_DIR / filename).write_bytes(content)
                actor["image_file"] = filename
                actor["notes"] = source_note
                real_photos += 1
            else:
                deleted_placeholders += _clear_actor_images(IMAGE_DIR, name)
                actor["image_file"] = None
                if notes_indicate_placeholder(notes) or not notes:
                    actor["notes"] = "No public portrait found; UI shows initials until upload."
                without_image += 1

        if not actor.get("biography"):
            cats = ", ".join(actor.get("categories") or [])  # type: ignore[arg-type]
            actor["biography"] = f"Seeded non-JAV performer ({cats})."

    referenced = {str(actor.get("image_file")) for actor in actors if actor.get("image_file")}
    for path_item in IMAGE_DIR.iterdir():
        if not path_item.is_file():
            continue
        if path_item.name in referenced:
            continue
        if is_solid_placeholder(path_item) or is_designed_identicon(path_item):
            path_item.unlink(missing_ok=True)
            deleted_placeholders += 1

    actors.sort(key=lambda item: str(item["name"]).casefold())
    catalog["actors"] = actors
    catalog["source"] = "avtor.txt+real-portraits"
    CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"actors={len(actors)} added={added} real_photos={real_photos} "
        f"kept_existing={kept_existing} without_image={without_image} "
        f"deleted_placeholders={deleted_placeholders} theporndb={'yes' if token else 'no'}"
    )


if __name__ == "__main__":
    main()
