# Copyright (C) 2021-2022 Edson Bernardino <edsones at yahoo.com.br>
# Copyright (C) 2024 Engenere - Antônio S. Pereira Neto <neto@engenere.one>

import re
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import Element

from fpdf import FontFace
from fpdf.enums import Align, VAlign

from ..utils import (
    chunks,
    format_cep,
    format_cpf_cnpj,
    format_number,
    format_phone,
    get_date_utc,
    get_tag_text,
    merge_if_different,
)
from ..xfpdf import xFPDF
from .config import DanfeConfig, InvoiceDisplay, ReceiptPosition
from .danfe_basic_field import DanfeBasicField
from .danfe_block import DanfeBlock
from .danfe_code import DanfeCode
from .danfe_conf import DEFAULT_FIELD_HEIGHT, HEIGHT_FONT_BLOCK_DESC, URL
from .danfe_emit_info import DanfeEmitInfo
from .danfe_ident_info import DanfeIdentInfo
from .danfe_verification_msg import DanfeVerificationMsg
from .models import BaseFieldInfo, ProductInfo

tp_frete = {
    "0": "0 - Remetente",
    "1": "1 - Destinatário",
    "2": "2 - Terceiros",
    "3": "3 - Próprio/Rem",
    "4": "4 - Próprio/Dest",
    "9": "9 - Sem Frete",
}


def extract_text(node: Element, tag: str) -> str:
    return get_tag_text(node, URL, tag)


class Danfe(xFPDF):
    def __init__(self, xml, config: DanfeConfig = None):
        super().__init__(unit="mm", format="A4")
        config = config if config is not None else DanfeConfig()
        self.set_margins(
            left=config.margins.left, top=config.margins.top, right=config.margins.right
        )
        self.set_auto_page_break(auto=False, margin=config.margins.bottom)
        self.set_title("DANFE")
        self.logo_image = config.logo
        self.receipt_pos = config.receipt_pos
        self.default_font = config.font_type.value
        self.price_precision = config.decimal_config.price_precision
        self.quantity_precision = config.decimal_config.quantity_precision
        self.invoice_display = config.invoice_display
        self.display_pis_cofins = config.display_pis_cofins
        self.product_description_config = config.product_description_config

        root = ET.fromstring(xml)
        self.inf_nfe = root.find(f"{URL}infNFe")
        self.prot_nfe = root.find(f"{URL}protNFe")
        self.emit = root.find(f"{URL}emit")
        self.ide = root.find(f"{URL}ide")
        self.dest = root.find(f"{URL}dest")
        self.retirada = root.find(f"{URL}retirada")
        self.entrega = root.find(f"{URL}entrega")
        self.totais = root.find(f"{URL}total")
        self.transp = root.find(f"{URL}transp")
        self.cobr = root.find(f"{URL}cobr")
        self.det = root.findall(f"{URL}det")
        self.inf_adic = root.find(f"{URL}infAdic")
        self.issqn_tot = root.find(f"{URL}ISSQNtot")
        self.crt = extract_text(self.emit, "CRT")

        self.total_receipt_height = 19  # TODO need compute

        # extract orientation
        tpImp = extract_text(self.ide, "tpImp")
        if tpImp == "1":
            self.orientation = "P"
        else:
            self.orientation = "L"
            # force receipt position
            # landscape support only left receipt
            self.receipt_pos = ReceiptPosition.LEFT

        # Emit. CNPJ/CPF
        self.emit_cnpj_cpf = extract_text(self.emit, "CNPJ")
        self.emit_id_label = "CNPJ"
        if not self.emit_cnpj_cpf:
            self.emit_id_label = "CPF"
            self.emit_cnpj_cpf = extract_text(self.emit, "CPF")
        self.emit_cnpj_cpf = format_cpf_cnpj(self.emit_cnpj_cpf)

        # Dest. CNPJ/CPF
        self.dest_cnpj_cpf = extract_text(self.dest, "CNPJ")
        self.dest_id_label = "CNPJ"
        if not self.dest_cnpj_cpf:
            self.dest_id_label = "CPF"
            self.dest_cnpj_cpf = extract_text(self.dest, "CPF")
        self.dest_cnpj_cpf = format_cpf_cnpj(self.dest_cnpj_cpf)

        self.recibo_text = self._get_receipt_text()
        self.nr_nota = extract_text(self.ide, "nNF")
        self.serie_nf = extract_text(self.ide, "serie")
        self.tp_nf = extract_text(self.ide, "tpNF")
        self.key_nfe = self.inf_nfe.attrib.get("Id")[3:]
        self.prot_uso = self._get_usage_protocol()
        self.products = self._get_products_info()

        self.add_page(orientation=self.orientation)

        # simulate render for get block sizes.
        addit_data = self._get_additional_data_content()
        with self._disable_writing():
            add_data_lines, max_add_data_lines = self._draw_additional_data(addit_data)
        addit_data_current_page = add_data_lines[:max_add_data_lines]
        addit_data_current_page = " ".join(addit_data_current_page)
        addit_data_next_pages = add_data_lines[max_add_data_lines:]
        addit_data_next_pages = " ".join(addit_data_next_pages)

        with self._disable_writing():
            y_before = self.y
            self._draw_header()
            header_height = self.y - y_before

        # blocks before products
        with self._disable_writing():
            y_before = self.get_y()
            if self.receipt_pos == ReceiptPosition.TOP:
                self._draw_receipt()
            self._draw_header()
            self._draw_recipient_sender()
            self._draw_delivery_location()
            self._draw_billing()
            self._draw_taxes()
            self._draw_shipping()
            y_after = self.get_y()
        height_before = y_after - y_before

        # blocks after products
        # ISSQN and DADOS ADICIONAIS
        with self._disable_writing():
            y_before = self.get_y()
            self._draw_issqn_calculation()
            self._draw_additional_data(addit_data_current_page)
            if self.receipt_pos == ReceiptPosition.BOTTOM:
                self._draw_receipt()
            y_after = self.get_y()
        height_after = y_after - y_before

        available_height_product_table = (
            self.eph - height_before - height_after - HEIGHT_FONT_BLOCK_DESC
        )

        (
            products_for_current_page,
            products_for_next_pages,
        ) = self._calculate_product_splits(
            products=self.products,
            height_product_table=available_height_product_table,
        )

        p_addit_data, addit_data_next_pages = self._split_additional_data_in_products(
            available_height_product_table,
            products_for_current_page,
            addit_data_next_pages,
        )

        # If there is a continuation of additional data and there is space left
        # below the products, write the continuation on the same page.
        # if addit_data_next_pages and not products_for_next_pages:

        # draw real pdf (first page)
        self._draw_void_watermark()
        if self.receipt_pos == ReceiptPosition.LEFT:
            self._draw_landscape_receipt()
        if self.receipt_pos == ReceiptPosition.TOP:
            self._draw_receipt()
        self._draw_header()
        self._draw_recipient_sender()
        self._draw_delivery_location()
        self._draw_billing()
        self._draw_taxes()
        self._draw_shipping()
        self._draw_products(
            available_height_product_table, products_for_current_page, p_addit_data
        )
        self._draw_issqn_calculation()
        self._draw_additional_data(addit_data_current_page)
        if self.receipt_pos == ReceiptPosition.BOTTOM:
            self._draw_receipt()

        # draw next pages, if necessary.
        while products_for_next_pages:
            self.add_page(orientation=self.orientation)
            self._draw_void_watermark()
            self._draw_header()
            height_product_table = self.eph - header_height - HEIGHT_FONT_BLOCK_DESC
            products_for_current_page, products_for_next_pages = (
                self._calculate_product_splits(
                    products=products_for_next_pages,
                    height_product_table=height_product_table,
                )
            )
            # check if have space below products to print additional data
            p_addit_data, addit_data_next_pages = (
                self._split_additional_data_in_products(
                    available_height_product_table,
                    products_for_current_page,
                    addit_data_next_pages,
                )
            )
            self._draw_products(
                height_product_table, products_for_current_page, p_addit_data
            )

        while addit_data_next_pages:
            # At this point, there is no product and service block to include the
            # continuation of the additional information, and the continuation is
            # carried out on the next page with the additional data block.
            self.add_page(orientation=self.orientation)
            self._draw_void_watermark()
            self._draw_header()
            height_additional_data = self.eph - header_height
            # simulate render in temp pdf for get block sizes.
            addit_data = self._get_additional_data_content()
            with self._disable_writing():
                add_data_lines, max_add_data_lines = self._draw_additional_data(
                    addit_data_next_pages, height_additional_data
                )
            addit_data_current_page = add_data_lines[:max_add_data_lines]
            addit_data_current_page = " ".join(addit_data_current_page)
            addit_data_next_pages = add_data_lines[max_add_data_lines:]
            addit_data_next_pages = " ".join(addit_data_next_pages)
            self._draw_additional_data(addit_data_current_page, height_additional_data)

    @property
    def edw(self):
        """
        Effective danfe width:
        In landscape orientation the page width minus its horizontal margins
        and receipt width.
        """
        if self.orientation == "L" and self.page_no() == 1:
            # TODO get receipt width
            return self.epw - 19
        else:
            return self.epw

    def _get_usage_protocol(self):
        dt, hr = get_date_utc(extract_text(self.prot_nfe, "dhRecbto"))
        protocol = extract_text(self.prot_nfe, "nProt")
        prot_text = f"{protocol} - {dt} {hr}"
        return prot_text

    def _get_receipt_text(self):
        dt, hr = get_date_utc(extract_text(self.ide, "dhEmi"))
        total_nf = format_number(extract_text(self.totais, "vNF"), precision=2)
        end = (
            f"{extract_text(self.dest,'xNome')} - "
            f"{extract_text(self.dest,'xLgr')}, "
            f"{extract_text(self.dest,'nro')}, "
            f"{extract_text(self.dest,'xBairro')}, "
            f"{extract_text(self.dest,'xMun')} - "
            f"{extract_text(self.dest,'UF')}"
        )
        receipt_text = (
            f"RECEBEMOS DE {extract_text(self.emit,'xNome')} "
            f"OS PRODUTOS/SERVIÇOS CONSTANTES DA NOTA FISCAL INDICADA "
            f"ABAIXO. EMISSÃO: {dt} VALOR TOTAL: {total_nf} DESTINATARIO: {end}"
            f", {self.dest_id_label}: {self.dest_cnpj_cpf}"
        )
        return receipt_text

    def _build_inf_ad_prod(self, prod, inf_ad_prod):
        add_infos = []
        _rastros = prod.findall(f"{URL}rastro")

        prefix = self.product_description_config.branch_info_prefix
        prefix = f"{prefix} " if prefix else ""
        if self.product_description_config.display_branch:
            for _rastro in _rastros:
                n_lote = extract_text(_rastro, "nLote")
                q_lote = format_number(
                    extract_text(_rastro, "qLote"), self.quantity_precision
                )
                d_fab, _ = get_date_utc(extract_text(_rastro, "dFab"))
                d_val, _ = get_date_utc(extract_text(_rastro, "dVal"))
                add_infos.append(
                    f"{prefix}Lote: {n_lote} Qtd: {q_lote} Fab: {d_fab} Val: {d_val}"
                )

        cbenef = extract_text(prod, "cBenef")
        if cbenef:
            add_infos.append(f"cBenef: {cbenef}")

        if self.product_description_config.display_additional_info and inf_ad_prod:
            add_infos.append(inf_ad_prod)

        add_infos_text = "\n".join(add_infos)
        return add_infos_text

    def _get_products_info(self):
        products = []
        for _det in self.det:
            el_prod = _det.find(f"{URL}prod")
            # el_imp = _det.find(f"{URL}imposto")
            el_imp_ICMS = _det.find(f"{URL}ICMS")
            el_imp_IPI = _det.find(f"{URL}IPI")

            inf_ad_prod = self._build_inf_ad_prod(
                el_prod, extract_text(_det, "infAdProd")
            )
            x_prod = extract_text(el_prod, "xProd")

            u_com = extract_text(el_prod, "uCom")
            q_com = format_number(
                extract_text(el_prod, "qCom"), self.quantity_precision
            )
            v_un_com = format_number(
                extract_text(el_prod, "vUnCom"), self.price_precision
            )

            u_trib = extract_text(el_prod, "uTrib")
            q_trib = format_number(
                extract_text(el_prod, "qTrib"), self.quantity_precision
            )
            v_un_trib = format_number(
                extract_text(el_prod, "vUnTrib"), self.price_precision
            )

            # merge commercial and taxable values
            unid = merge_if_different(u_com, u_trib)
            qty = merge_if_different(q_com, q_trib)
            unit_price = merge_if_different(v_un_com, v_un_trib)

            # merge 'origem' with 'CST' of ICMS.
            orig = extract_text(el_imp_ICMS, "orig")
            if self.crt == "1":
                # Regime Simples Nacional
                cst = extract_text(el_imp_ICMS, "CSOSN")
            else:
                # Regime Normal
                cst = extract_text(el_imp_ICMS, "CST")
            o_cst = orig + cst

            product = ProductInfo(
                code=extract_text(el_prod, "cProd"),
                description=self._merge_product_description(x_prod, inf_ad_prod),
                ncm_sh=extract_text(el_prod, "NCM"),
                cst=o_cst,
                cfop=extract_text(el_prod, "CFOP"),
                unid=unid,
                qty=qty,
                unit_price=unit_price,
                total_price=format_number(extract_text(el_prod, "vProd"), 2),
                bs_icms=format_number(extract_text(el_imp_ICMS, "vBC"), 2),
                icms_value=format_number(extract_text(el_imp_ICMS, "vICMS"), 2),
                ipi_value=format_number(extract_text(el_imp_IPI, "vIPI"), 2),
                icms_rate=format_number(extract_text(el_imp_ICMS, "pICMS"), 2),
                ipi_rate=format_number(extract_text(el_imp_IPI, "pIPI"), 2),
            )
            products.append(product)
        return products

    def _get_additional_data_content(self):
        fisco = extract_text(self.inf_adic, "infAdFisco")
        obs = extract_text(self.inf_adic, "infCpl")
        dest_end, cpl, cpl_truncado = self._get_dest_end_text(self.dest)
        if cpl_truncado:
            obs += "Complemento do destinatário: " + cpl + "."
        if fisco:
            obs = f"{obs} {fisco}\n"
        obs = " ".join(re.split(r"\s+", obs.strip(), flags=re.UNICODE))
        return obs

    def _calculate_product_splits(self, products, height_product_table):
        """
        Splits a list of products into two lists based on the maximum available
        height for a product table, ensuring that the split respects the maximum
        allowed height to prevent overlap or cutting of products in the table
        display.

        During the calculation, writing is temporarily disabled to avoid
        modifications to the current document. This allows for a simulation of
        product drawing to determine how they should be divided between pages.

        Args:
            products (list): A list of products to be drawn in the table.
                Each product should be a data structure containing necessary
                information for drawing the product in the table.
            height_product_table (float):
                The maximum available height for the product table on a single page,
                determining how the products should be divided between pages.

        Returns:
            tuple: Two lists of products, where the first list contains the
                products that fit within the available height of the current page
                and the second list contains the products that should be moved to
                the next pages. Each list contains subsets of the original product
                list, divided based on the maximum allowed height.
        """
        with self._disable_writing():
            row_info_list = self._draw_products(height_product_table, products)[0]
        product_header_height = row_info_list.pop(0).height
        actual_height = product_header_height
        product_index = 0
        for i, row_info in enumerate(row_info_list):
            actual_height += row_info.height
            if actual_height <= height_product_table:
                product_index = i
            else:
                break
        products_for_current_page = products[: product_index + 1]
        products_for_next_pages = products[product_index + 1 :]
        return (
            products_for_current_page,
            products_for_next_pages,
        )

    def _merge_product_description(self, x_prod, inf_ad_prod):
        desc = x_prod
        if inf_ad_prod:
            desc += "\n" + inf_ad_prod
        # normalize
        # desc = " ".join(re.split(r"\s+", desc.strip(), flags=re.UNICODE))
        return desc

    def _split_additional_data_in_products(
        self,
        available_height_product_table,
        products_for_current_page,
        addit_data_next_pages,
    ):
        addit_data = None
        if addit_data_next_pages:
            with self._disable_writing():
                _, current_add_info_lines, max_add_info_lines = self._draw_products(
                    available_height_product_table,
                    products_for_current_page,
                    addit_data_next_pages,
                )
            if max_add_info_lines > 1:
                if len(current_add_info_lines) > max_add_info_lines:
                    # split
                    addit_data = current_add_info_lines[:max_add_info_lines]
                    addit_data = " ".join(addit_data)
                    addit_data_next_pages = current_add_info_lines[max_add_info_lines:]
                    addit_data_next_pages = " ".join(addit_data_next_pages)
                else:
                    # not split
                    addit_data = " ".join(current_add_info_lines)
                    addit_data_next_pages = []
        return addit_data, addit_data_next_pages

    def _draw_void_watermark(self):
        """
        Draw a watermark on the DANFE when the protocol is not available or
        when the environment is homologation.
        """
        is_production_environment = extract_text(self.ide, "tpAmb") == "1"
        is_protocol_available = bool(self.prot_nfe)

        # Exit early if no watermark is needed
        if is_production_environment and is_protocol_available:
            return

        self.set_font(self.default_font, "B", 60)
        watermark_text = "SEM VALOR FISCAL"
        width = self.get_string_width(watermark_text)
        self.set_text_color(r=220, g=150, b=150)
        height = 15
        page_width = self.w
        page_height = self.h
        x_center = (page_width - width) / 2
        y_center = (page_height + height) / 2
        with self.rotation(55, x_center + (width / 2), y_center - (height / 2)):
            self.text(x_center, y_center, watermark_text)
        self.set_text_color(r=0, g=0, b=0)

    def _draw_dashed_line(self, distance):
        self.set_dash_pattern(dash=0.2, gap=0.8)
        if self.orientation == "P":
            self.line(
                x1=self.l_margin,
                y1=distance,
                x2=self.w - self.r_margin,
                y2=distance,
            )
        else:
            self.line(
                x1=distance,
                y1=self.t_margin,
                x2=distance,
                y2=self.h - self.b_margin,
            )
        self.set_dash_pattern(dash=0, gap=0)

    def _draw_landscape_receipt(self):
        h_recibo = 17
        lin = self.y
        self.set_dash_pattern(dash=0, gap=0)
        self.rect(x=self.l_margin, y=self.t_margin, w=h_recibo, h=self.eph, style="")

        # fields width
        w_number_field = 30
        w_date_field = 40
        w_sign_field = self.eph - w_date_field - w_number_field
        w_desc_field = w_date_field + w_sign_field

        y_number_field = self.t_margin + w_number_field
        # column partition line
        self.line(
            x1=self.l_margin,
            y1=y_number_field,
            x2=self.l_margin + h_recibo,
            y2=y_number_field,
        )

        # partition recibo in two lines
        x_line = lin + h_recibo / 2
        self.line(
            x1=x_line,
            y1=lin + w_number_field,
            x2=x_line,
            y2=lin + w_number_field + w_desc_field,
        )

        w_date_field = 40  # width of field "data de recebimento"
        # line between the field 'data' and 'assinatura'
        line_y = self.b_margin + w_number_field + w_sign_field
        self.line(x1=lin + h_recibo / 2, y1=line_y, x2=lin + h_recibo, y2=line_y)
        self.set_font(self.default_font, "", 5)
        h_text = 2
        self.set_xy(x=lin + 1, y=self.eph + h_text)
        with self.rotation(90):
            self.multi_cell(
                w=w_desc_field, h=h_text, text=self.recibo_text, border=0, align="L"
            )
        self.set_xy(x=lin + h_recibo / 2 + 0.5, y=self.eph + h_text)
        with self.rotation(90):
            self.cell(
                w=w_date_field,
                h=h_text,
                text="DATA DE RECEBIMENTO",
                new_x="RIGHT",
                align="L",
            )
            self.cell(
                w=None,
                h=h_text,
                text="IDENTIFICAÇÃO E ASSINATURA DO RECEBEDOR",
                new_x="LEFT",
                align="L",
            )

        # format nf number
        nf = f"{int(self.nr_nota):,}".replace(",", ".")

        self.set_font(self.default_font, "B", 8)
        text = f"NOTA FISCAL\n\nNº {nf}\n" f"\nSÉRIE {self.serie_nf}"
        self.text_box(
            text=text,
            text_align="C",
            w=h_recibo,
            h=w_number_field,
            h_line=3,
            x=self.l_margin,
            y=self.t_margin,
        )
        self._draw_dashed_line(distance=lin + h_recibo + 1)
        self.set_xy(x=self.l_margin + h_recibo + 2, y=self.t_margin)

    def _draw_receipt(self):
        h_recibo = 17
        lin = self.y
        if self.receipt_pos == ReceiptPosition.BOTTOM:
            self._draw_dashed_line(distance=self.y + 1)
            lin += 2

        self.set_dash_pattern(dash=0, gap=0)
        self.rect(x=self.l_margin, y=lin, w=self.edw, h=h_recibo, style="")

        # fields width
        w_number_field = 30
        w_date_field = 40
        w_sign_field = self.epw - w_date_field - w_number_field
        w_desc_field = w_date_field + w_sign_field

        x_number_field = self.l_margin + w_desc_field
        # column partition line
        self.line(x1=x_number_field, y1=lin, x2=x_number_field, y2=lin + h_recibo)

        # partition recibo in two lines
        self.line(self.l_margin, lin + h_recibo / 2, x_number_field, lin + 8.5)

        w_date_field = 40  # width of field "data de recebimento"
        # line between the field 'data' and 'assinatura'
        line_y = self.l_margin + w_date_field
        self.line(line_y, lin + h_recibo / 2, line_y, lin + h_recibo)

        self.set_font(self.default_font, "", 5)
        self.set_xy(x=self.l_margin, y=lin + 1)
        self.multi_cell(
            w=w_desc_field, h=None, text=self.recibo_text, border=0, align="L"
        )
        self.set_xy(x=self.l_margin, y=lin + h_recibo / 2 + 0.5)
        self.cell(
            w=w_date_field, h=None, text="DATA DE RECEBIMENTO", new_x="RIGHT", align="L"
        )
        self.cell(
            w=None,
            h=None,
            text="IDENTIFICAÇÃO E ASSINATURA DO RECEBEDOR",
            new_x="LEFT",
            align="L",
        )

        self.set_font(self.default_font, "B", 10)
        self.set_xy(x=x_number_field, y=lin + 0.5)
        self.cell(
            w=w_number_field,
            h=None,
            text="NF-e",
            new_x="LEFT",
            new_y="NEXT",
            align="C",
        )
        nf = f"{int(self.nr_nota):011,}".replace(",", ".")
        self.cell(
            w=w_number_field,
            h=6,
            text=f"Nº{nf}",
            new_x="LEFT",
            new_y="NEXT",
            align="C",
        )
        self.cell(
            w=w_number_field,
            h=None,
            text=f"SÉRIE {self.serie_nf}",
            align="C",
        )
        if self.receipt_pos == ReceiptPosition.TOP:
            self._draw_dashed_line(distance=lin + h_recibo + 1)
            lin += 2
        self.set_xy(x=self.l_margin, y=lin + h_recibo)

    def _draw_header(self):
        # pre-definitions
        w_ident_box = 33
        w_code_box = 88
        w_emit_box = self.edw - w_ident_box - w_code_box
        h_emit_box = 31
        old_y = self.get_y()
        emit_name = extract_text(self.emit, "xNome")
        cep = format_cep(extract_text(self.emit, "CEP"))
        fone = format_phone(extract_text(self.emit, "fone"))
        xCpl = (
            f"{extract_text(self.emit, 'xCpl')}\n"
            if extract_text(self.emit, "xCpl")
            else ""
        )
        address = (
            f"{extract_text(self.emit,'xLgr')}, "
            f"{extract_text(self.emit,'nro')}\n"
            f"{xCpl}"
            f"{extract_text(self.emit,'xBairro')}\n"
            f"{extract_text(self.emit,'xMun')} - "
            f"{extract_text(self.emit,'UF')}\n"
            f"{cep}\nFone: {fone}"
        )
        b_emit = DanfeBlock(pdf=self)
        e_emit_info = DanfeEmitInfo(
            h=h_emit_box,
            w=w_emit_box,
            new_x="RIGHT",
            new_y="TOP",
            emit=emit_name,
            logo_image=self.logo_image,
            address=address,
            pdf=self,
        )
        b_emit.add_field(e_emit_info)
        e_ident_info = DanfeIdentInfo(
            h=h_emit_box,
            w=w_ident_box,
            new_x="RIGHT",
            new_y="TOP",
            serie_nf=self.serie_nf,
            nr_nota=self.nr_nota,
            tp_nf=self.tp_nf,
            pdf=self,
        )
        b_emit.add_field(e_ident_info)
        e_danfe_code = DanfeCode(
            h=10,
            w=w_code_box,
            new_x="LEFT",
            new_y="BOTTOM",
            key_nfe=self.key_nfe,
            pdf=self,
        )
        b_emit.add_field(e_danfe_code)
        f_chave_acesso = DanfeBasicField(
            w=w_code_box,
            description="CHAVE DE ACESSO",
            content=" ".join(chunks(self.key_nfe, 4)),
            type="chave_acesso",
            new_x="LEFT",
            new_y="BOTTOM",
            pdf=self,
        )
        b_emit.add_field(f_chave_acesso)
        f_autenticidade_msg = DanfeVerificationMsg(
            w=f_chave_acesso.w, h=15, new_x="L_BLOCK", new_y="BOTTOM", pdf=self
        )
        b_emit.add_field(f_autenticidade_msg)

        self.y = old_y + h_emit_box
        text_nat_op = extract_text(self.ide, "natOp")
        f_nat_op = DanfeBasicField(
            w=w_emit_box + w_ident_box,
            description="NATUREZA DA OPERAÇÃO",
            content=text_nat_op,
            pdf=self,
        )
        b_emit.add_field(f_nat_op)
        f_prot = DanfeBasicField(
            w=w_code_box,
            description="PROTOCOLO DE AUTORIZAÇÃO DE USO",
            content=self.prot_uso,
            type="protocolo",
            new_x="L_BLOCK",
            new_y="BOTTOM",
            pdf=self,
        )
        b_emit.add_field(f_prot)
        f_emit_ie = DanfeBasicField(
            w=b_emit.w / 3,
            description="INSCRIÇÃO ESTADUAL",
            content=extract_text(self.emit, "IE"),
            pdf=self,
        )
        b_emit.add_field(f_emit_ie)
        f_emit_ie_st = DanfeBasicField(
            w=b_emit.w / 3,
            description="INSCRIÇÃO ESTADUAL DO SUBST. TRIB",
            content=extract_text(self.emit, "IEST"),
            pdf=self,
        )
        b_emit.add_field(f_emit_ie_st)
        f_emit_cnpj = DanfeBasicField(
            w=b_emit.w - f_emit_ie.w - f_emit_ie_st.w,
            description="CNPJ / CPF",
            content=self.emit_cnpj_cpf,
            pdf=self,
        )
        b_emit.add_field(f_emit_cnpj)
        b_emit.render()

    def _draw_recipient_sender(self):
        # get content data
        if extract_text(self.ide, "tpAmb") == "1":
            dest_name = extract_text(self.dest, "xNome")
        else:
            dest_name = "NF-E EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL"
        dest_cnpj_cpf = extract_text(self.dest, "CNPJ")
        if not dest_cnpj_cpf:
            dest_cnpj_cpf = extract_text(self.dest, "CPF")
        dest_cnpj_cpf = format_cpf_cnpj(dest_cnpj_cpf)
        date_emi, time_emi = get_date_utc(extract_text(self.ide, "dhEmi"))
        dest_end = self._get_dest_end_text(self.dest)[0]
        dest_bairro = extract_text(self.dest, "xBairro")
        dest_cep = extract_text(self.dest, "CEP")
        dest_cep = format_cep(dest_cep)
        date_sai_ent, time_sai_ent = get_date_utc(extract_text(self.ide, "dhSaiEnt"))
        dest_mun = extract_text(self.dest, "xMun")
        dest_fone = extract_text(self.dest, "fone")
        dest_fone = format_phone(dest_fone)
        dest_uf = extract_text(self.dest, "UF")
        dest_ie = extract_text(self.dest, "IE")

        block_dest = DanfeBlock(
            description="DESTINATÁRIO / REMETENTE",
            pdf=self,
        )

        # pre-definitions line 1
        w_dest_cnpj = 35
        w_data_emi = 30
        w_dest_name = block_dest.w - w_dest_cnpj - w_data_emi

        block_dest.add_field(
            DanfeBasicField(
                w=w_dest_name,
                description="NOME / RAZÃO SOCIAL",
                content=dest_name,
                pdf=self,
            )
        )
        block_dest.add_field(
            DanfeBasicField(
                w=w_dest_cnpj, description="CNPJ / CPF", content=dest_cnpj_cpf, pdf=self
            )
        )
        block_dest.add_field(
            DanfeBasicField(
                w=w_data_emi,
                description="DATA DA EMISSÃO",
                content=date_emi,
                new_x="L_BLOCK",
                new_y="BOTTOM",
                pdf=self,
            )
        )

        # pre-definitions line 2
        w_dest_bairro = 50
        w_data_cep = 25
        w_data_ent_sai = 30
        w_dest_end = block_dest.w - w_dest_bairro - w_data_cep - w_data_ent_sai

        block_dest.add_field(
            DanfeBasicField(
                w=w_dest_end, description="ENDEREÇO", content=dest_end, pdf=self
            )
        )
        block_dest.add_field(
            DanfeBasicField(
                w=w_dest_bairro,
                description="BAIRRO / DISTRITO",
                content=dest_bairro,
                pdf=self,
            )
        )
        block_dest.add_field(
            DanfeBasicField(w=w_data_cep, description="CEP", content=dest_cep, pdf=self)
        )
        block_dest.add_field(
            DanfeBasicField(
                w=w_data_ent_sai,
                description="DATA DA ENTRADA / SAÍDA",
                content=date_sai_ent,
                new_x="L_BLOCK",
                new_y="BOTTOM",
                pdf=self,
            )
        )

        widths_3 = {"fone": 40, "uf": 10, "ie": 50, "hora_emit": 30}
        width_disponible = block_dest.w - sum(widths_3.values())
        widths_3["municipio"] = width_disponible

        block_dest.add_field(
            DanfeBasicField(
                w=widths_3["municipio"],
                description="MUNICÍPIO",
                content=dest_mun,
                pdf=self,
            )
        )
        block_dest.add_field(
            DanfeBasicField(
                w=widths_3["fone"],
                description="FONE / FAX",
                content=dest_fone,
                pdf=self,
            )
        )
        block_dest.add_field(
            DanfeBasicField(
                w=widths_3["uf"], description="UF", content=dest_uf, pdf=self
            )
        )
        block_dest.add_field(
            DanfeBasicField(
                w=widths_3["ie"],
                description="INSCRIÇÃO ESTADUAL",
                content=dest_ie,
                pdf=self,
            )
        )
        block_dest.add_field(
            DanfeBasicField(
                w=widths_3["hora_emit"],
                description="HORA DE ENTRADA / SAÍDA",
                content=time_sai_ent,
                pdf=self,
            )
        )
        block_dest.render()

    def _draw_delivery_location(self):
        if self.entrega is not None and len(self.entrega):
            elem = self.entrega
            description = "INFORMAÇÕES DO LOCAL DE ENTREGA"
        elif self.retirada is not None and len(self.retirada):
            elem = self.retirada
            description = "INFORMAÇÕES DO LOCAL DE RETIRADA"
        else:
            return

        # Get Content Data
        name = extract_text(elem, "xNome")
        cnpj_cpf = extract_text(elem, "CNPJ")
        if not cnpj_cpf:
            cnpj_cpf = extract_text(elem, "CPF")
        cnpj_cpf = format_cpf_cnpj(cnpj_cpf)
        ie = extract_text(elem, "IE")
        endereco = self._get_dest_end_text(elem)[0]

        bairro = extract_text(elem, "xBairro")
        cep = extract_text(elem, "CEP")
        cep = format_cep(cep)
        municipio = extract_text(elem, "xMun")
        uf = extract_text(elem, "UF")
        fone = extract_text(elem, "fone")
        fone = format_phone(fone)

        # BLOCO LOCAL DE ENTREGA OU RETIRADA
        block_entrega = DanfeBlock(
            description=description,
            pdf=self,
        )

        # sizes pre-definitions
        widths = {
            "line1": {"cnpj_cpf": 35, "ie": 30},
            "line2": {"bairro": 75, "cep": 30},
            "line3": {
                "fone": 30,
                "uf": 10,
            },
        }
        widths["line1"]["name"] = block_entrega.w - sum(widths["line1"].values())
        widths["line2"]["endereco"] = block_entrega.w - sum(widths["line2"].values())
        widths["line3"]["municipio"] = block_entrega.w - sum(widths["line3"].values())

        block_entrega.add_field(
            DanfeBasicField(
                w=widths["line1"]["name"],
                description="NOME / RAZÃO SOCIAL",
                content=name,
                pdf=self,
            )
        )
        block_entrega.add_field(
            DanfeBasicField(
                w=widths["line1"]["cnpj_cpf"],
                description="CNPJ / CPF",
                content=cnpj_cpf,
                pdf=self,
            )
        )
        block_entrega.add_field(
            DanfeBasicField(
                w=widths["line1"]["ie"],
                description="IE",
                content=ie,
                new_x="L_BLOCK",
                new_y="BOTTOM",
                pdf=self,
            )
        )
        block_entrega.add_field(
            DanfeBasicField(
                w=widths["line2"]["endereco"],
                description="ENDEREÇO",
                content=endereco,
                pdf=self,
            )
        )
        block_entrega.add_field(
            DanfeBasicField(
                w=widths["line2"]["bairro"],
                description="BAIRRO / DISTRITO",
                content=bairro,
                pdf=self,
            )
        )
        block_entrega.add_field(
            DanfeBasicField(
                w=widths["line2"]["cep"],
                description="CEP",
                new_x="L_BLOCK",
                new_y="BOTTOM",
                content=cep,
                pdf=self,
            )
        )
        block_entrega.add_field(
            DanfeBasicField(
                w=widths["line3"]["municipio"],
                description="MUNICÍPIO",
                content=municipio,
                pdf=self,
            )
        )
        block_entrega.add_field(
            DanfeBasicField(
                w=widths["line3"]["uf"], description="UF", content=uf, pdf=self
            )
        )
        block_entrega.add_field(
            DanfeBasicField(
                w=widths["line3"]["fone"], description="FONE", content=fone, pdf=self
            )
        )
        block_entrega.render()

    def _draw_billing(self):
        if not self.cobr:
            # Skip
            return

        # Fatura Block
        block_fatura = DanfeBlock(
            description="FATURA / DUPLICATAS",
            pdf=self,
        )

        fat = self.cobr.find(f"{URL}fat")
        dup = self.cobr.findall(f"{URL}dup")

        # Content Data
        numero = extract_text(fat, "nFat")
        valor_original = format_number(extract_text(fat, "vOrig"), 2)
        Valor_desconto = format_number(extract_text(fat, "vDesc"), 2)
        valor_liquido = format_number(extract_text(fat, "vLiq"), 2)

        # Pre Definitions Sizes
        w_numero = w_original = w_desconto = block_fatura.w / 4
        w_liquido = block_fatura.w - w_numero - w_original - w_desconto

        if self.invoice_display == InvoiceDisplay.FULL_DETAILS:
            block_fatura.add_field(
                DanfeBasicField(
                    w=w_numero, description="NÚMERO", content=numero, pdf=self
                )
            )
            block_fatura.add_field(
                DanfeBasicField(
                    w=w_original,
                    type="number",
                    description="VALOR ORIGINAL",
                    content=valor_original,
                    pdf=self,
                )
            )
            block_fatura.add_field(
                DanfeBasicField(
                    w=w_desconto,
                    type="number",
                    description="VALOR DO DESCONTO",
                    content=Valor_desconto,
                    pdf=self,
                )
            )
            block_fatura.add_field(
                DanfeBasicField(
                    w=w_liquido,
                    type="number",
                    description="VALOR LÍQUIDO",
                    content=valor_liquido,
                    pdf=self,
                )
            )
        block_fatura.render()

        if not dup:
            # Skip
            return

        self.set_font(self.default_font, "", 7)
        dups_text = []
        max_width = 0.0

        # Single loop to create `duplicatas` texts and find the maximum width
        for _item_dup in dup:
            num = extract_text(_item_dup, "nDup")
            venc = extract_text(_item_dup, "dVenc")
            venc, hr = get_date_utc(venc)
            valor = extract_text(_item_dup, "vDup")
            valor = format_number(valor, 2)
            dup_text = f"{num}  {venc}  {valor}"
            dups_text.append(dup_text)
            w_dup_text = self.get_string_width(dup_text) + 2
            max_width = max(max_width, w_dup_text)

        # Calculates the number of `duplicatas` that can fit in one line,
        # based on the maximum width
        qty_dup_line = int(block_fatura.w / max_width)

        # Division and display of `duplicatas` by lines
        for i in range(0, len(dups_text), qty_dup_line):
            line_dups = dups_text[i : i + qty_dup_line]
            # Fill the remaining cells of the line with empty text,
            # if necessary
            line_dups += [""] * (qty_dup_line - len(line_dups))
            old_x = self.x
            for _y, dup_text in enumerate(line_dups):
                self.cell(
                    block_fatura.w / qty_dup_line,
                    3,
                    dup_text,
                    border=1,
                    align="L",
                )
            self.ln()  # Line break after each group of `duplicatas`
            self.x = old_x  # fix start left position

    def _draw_taxes(self):
        block_impostos = DanfeBlock(
            description="CÁLCULO DO IMPOSTO",
            rows_heights=(
                DEFAULT_FIELD_HEIGHT,
                DEFAULT_FIELD_HEIGHT,
            ),
            pdf=self,
        )

        # Content Data
        v_bc = format_number(extract_text(self.totais, "vBC"), precision=2)
        v_icms = format_number(extract_text(self.totais, "vICMS"), precision=2)
        v_bcst = format_number(extract_text(self.totais, "vBCST"), precision=2)
        v_st = format_number(extract_text(self.totais, "vST"), precision=2)
        v_pis = format_number(extract_text(self.totais, "vPIS"), precision=2)
        v_prod = format_number(extract_text(self.totais, "vProd"), precision=2)
        v_frete = format_number(extract_text(self.totais, "vFrete"), precision=2)
        v_seg = format_number(extract_text(self.totais, "vSeg"), precision=2)
        v_desc = format_number(extract_text(self.totais, "vDesc"), precision=2)
        v_outro = format_number(extract_text(self.totais, "vOutro"), precision=2)
        v_ipi = format_number(extract_text(self.totais, "vIPI"), precision=2)
        v_confins = format_number(extract_text(self.totais, "vCOFINS"), precision=2)
        v_nf = format_number(extract_text(self.totais, "vNF"), precision=2)
        v_tot_trib = format_number(extract_text(self.totais, "vTotTrib"), precision=2)

        fields_line1 = [
            BaseFieldInfo(
                w=30, description="BASE DE CÁLCULO DO ICMS", content=v_bc, type="number"
            ),
            BaseFieldInfo(
                w=30, description="VALOR DO ICMS", content=v_icms, type="number"
            ),
            BaseFieldInfo(
                w=30,
                description="BASE DE CÁLCULO DO ICMS ST",
                content=v_bcst,
                type="number",
            ),
            BaseFieldInfo(
                w=30, description="VALOR DO ICMS ST ", content=v_st, type="number"
            ),
            BaseFieldInfo(
                w=30,
                description="VALOR APROX. TRIBUTOS",
                content=v_tot_trib,
                type="number",
            ),
            BaseFieldInfo(
                w=0,
                description="VALOR TOTAL DOS PRODUTOS",
                content=v_prod,
                type="number",
            ),
        ]
        fields_line2 = [
            BaseFieldInfo(
                w=30, description="VALOR DO FRETE", content=v_frete, type="number"
            ),
            BaseFieldInfo(
                w=30, description="VALOR DO SEGURO", content=v_seg, type="number"
            ),
            BaseFieldInfo(w=30, description="DESCONTO", content=v_desc, type="number"),
            BaseFieldInfo(
                w=30,
                description="OUTRAS DESPESAS ACESSÓRIAS",
                content=v_outro,
                type="number",
            ),
            BaseFieldInfo(
                w=30, description="VALOR DO IPI", content=v_ipi, type="number"
            ),
            BaseFieldInfo(
                w=0, description="VALOR TOTAL DA NOTA", content=v_nf, type="number"
            ),
        ]
        if self.display_pis_cofins:
            fields_line1.insert(
                -1,
                BaseFieldInfo(
                    w=0, description="VALOR DO PIS", content=v_pis, type="number"
                ),
            )
            fields_line2.insert(
                -1,
                BaseFieldInfo(
                    w=0, description="VALOR DO COFINS", content=v_confins, type="number"
                ),
            )
        block_impostos.add_fields([fields_line1, fields_line2])
        block_impostos.render()

    def _draw_shipping(self):
        block_transporte = DanfeBlock(
            rows_heights=(
                DEFAULT_FIELD_HEIGHT,
                DEFAULT_FIELD_HEIGHT,
                DEFAULT_FIELD_HEIGHT,
            ),
            description="TRANSPORTADOR / VOLUMES TRANSPORTADOS",
            pdf=self,
        )
        self.set_font(self.default_font, style="", size=5)

        # Content Data
        tp_frete_text = tp_frete[extract_text(self.transp, "modFrete")]
        transporta = self.transp.find(f"{URL}transporta")
        cnpj_cpf = extract_text(transporta, "CNPJ")
        if not cnpj_cpf:
            cnpj_cpf = extract_text(transporta, "CPF")
        cnpj_cpf = format_cpf_cnpj(cnpj_cpf)
        name = extract_text(transporta, "xNome")
        name = self.long_field(text=name, limit=60)
        ie = extract_text(transporta, "IE")
        ender = extract_text(transporta, "xEnder")
        ender = self.long_field(text=ender, limit=60)
        municipio = extract_text(transporta, "xMun")
        municipio = self.long_field(text=municipio, limit=60)
        uf = extract_text(transporta, "UF")
        veic_transp = self.transp.find(f"{URL}veicTransp")
        veic_placa = extract_text(veic_transp, "placa")
        veic_uf = extract_text(veic_transp, "UF")
        veic_rntc = extract_text(veic_transp, "RNTC")
        vol = self.transp.find(f"{URL}vol")
        q_vol = extract_text(vol, "qVol")
        esp = extract_text(vol, "esp")
        marca = extract_text(vol, "marca")
        n_vol = extract_text(vol, "nVol")
        peso_b = format_number(extract_text(vol, "pesoB"), precision=3)
        peso_l = format_number(extract_text(vol, "pesoL"), precision=3)

        fields_line1 = [
            BaseFieldInfo(w=0, description="NOME / RAZÃO SOCIAL", content=name),
            BaseFieldInfo(w=28, description="FRETE POR CONTA", content=tp_frete_text),
            BaseFieldInfo(w=18, description="CÓDIGO ANTT", content=veic_rntc),
            BaseFieldInfo(w=23, description="PLACA DO VEÍCULO", content=veic_placa),
            BaseFieldInfo(w=8, description="UF", content=veic_uf),
            BaseFieldInfo(w=30, description="CNPJ / CPF", content=cnpj_cpf),
        ]

        fields_line2 = [
            BaseFieldInfo(w=0, description="ENDEREÇO", content=ender),
            BaseFieldInfo(w=69, description="MUNICÍPIO", content=municipio),
            BaseFieldInfo(w=8, description="UF", content=uf),
            BaseFieldInfo(w=30, description="INSCRIÇÃO ESTADUAL", content=ie),
        ]

        fields_line3 = [
            BaseFieldInfo(w=25, description="QUANTIDADE", content=q_vol),
            BaseFieldInfo(w=30, description="ESPÉCIE", content=esp),
            BaseFieldInfo(w=30, description="MARCA", content=marca),
            BaseFieldInfo(w=45, description="NUMERAÇÃO", content=n_vol),
            BaseFieldInfo(w=0, description="PESO BRUTO", content=peso_b),
            BaseFieldInfo(w=0, description="PESO LÍQUIDO", content=peso_l),
        ]

        block_transporte.add_fields([fields_line1, fields_line2, fields_line3])
        block_transporte.render()

    def _draw_products(self, height_product_table, products, additional_data=""):
        DanfeBlock(
            description="DADOS DO PRODUTO / SERVIÇO",
            pdf=self,
        ).render()
        cst_label = "CST"
        cst_width = 6
        if self.crt == "1":
            # Regime Simples Nacional
            cst_label = "CSOSN"
            cst_width = 8
        colunas = [
            "CÓDIGO",
            "DESCRIÇÃO DOS PRODUTOS / SERVIÇOS",
            "NCM/SH",
            cst_label,
            "CFOP",
            "UN.",
            "QTD.",
            "V.UNIT.",
            # "DESCONTO",
            "V.TOTAL",
            "BC.ICMS",
            # "B.CÁLC.ICMS ST",
            # "VALOR ICMS ST",
            "V.ICMS",
            "V.IPI",
            "%ICMS",
            "%IPI",
        ]
        monetary_fields_index = [6, 7, 8, 9, 10, 11, 12, 13]
        col_widths = (15, None, 11, cst_width, 7, 6, 12, 13, 13, 13, 10, 10, 9, 8)
        defined_width = sum(filter(None, col_widths))
        none_width = self.edw - defined_width
        fixed_col_widths = tuple(w if w is not None else none_width for w in col_widths)
        y_before = self.get_y()
        x_before = self.get_x()
        self.set_font(self.default_font, "", 6)
        title_style = FontFace(emphasis="BOLD", size_pt=5)
        with self.table(
            col_widths=fixed_col_widths, line_height=3, width=self.edw, align="R"
        ) as table:
            row = table.row()
            for coluna in colunas:
                row.cell(text=coluna, style=title_style, v_align=VAlign.T)
            for product in products:
                row = table.row()
                for i, value in enumerate(product):
                    align = Align.R if i in monetary_fields_index else Align.L
                    row.cell(text=value, align=align, v_align=VAlign.T)
        # restore x position
        self.x = x_before

        product_height = self.get_y() - y_before
        h = height_product_table - product_height

        old_y = self.get_y()
        add_info_lines = max_add_info_lines = None
        if additional_data:
            add_info_field = DanfeBasicField(
                description="CONTINUAÇÃO DAS INFORMAÇÕES COMPLEMENTARES",
                content=additional_data,
                h=h,
                pdf=self,
                w=self.edw,
                x=self.get_x(),
                y=self.y,
            )
            add_info_field.render()
            add_info_lines = add_info_field.get_content_lines()
            max_add_info_lines = add_info_field.get_max_content_lines()
        else:
            self.rect(x=self.x, y=self.y, w=self.edw, h=h)
        self.y = old_y + h
        self.x = x_before

        # return info with rows heights
        row_info = list(table._process_rowpans_entries())
        return row_info, add_info_lines, max_add_info_lines

    def _draw_issqn_calculation(self):
        if not self.issqn_tot:
            return
        # content data
        im = extract_text(self.emit, "IM")
        v_serv = extract_text(self.issqn_tot, "vServ")
        v_bc = extract_text(self.issqn_tot, "vBC")
        v_iss = extract_text(self.issqn_tot, "vISS")

        block_issqn = DanfeBlock(
            rows_heights=(DEFAULT_FIELD_HEIGHT,),
            description="CÁLCULO DO ISSQN",
            pdf=self,
        )
        fields = [
            BaseFieldInfo(w=0, description="INSCRIÇÃO MUNICIPAL", content=im),
            BaseFieldInfo(w=45, description="VALOR TOTAL DOS SERVIÇOS", content=v_serv),
            BaseFieldInfo(w=45, description="BASE DO CÁLCULO DO ISSQN", content=v_bc),
            BaseFieldInfo(w=45, description="VALOR DO ISSQN", content=v_iss),
        ]
        block_issqn.add_fields([fields])
        block_issqn.render()

    def _draw_additional_data(self, additional_data, continuation_height=None):
        block_adic = DanfeBlock(
            description="DADOS ADICIONAIS",
            pdf=self,
        )
        height = (
            continuation_height - HEIGHT_FONT_BLOCK_DESC if continuation_height else 20
        )
        block_adic.rows_heights = (height,)
        if not continuation_height:
            fields = [
                BaseFieldInfo(
                    w=0,
                    description="INFORMAÇÕES COMPLEMENTARES",
                    content=additional_data,
                ),
                BaseFieldInfo(w=70, description="RESERVADO AO FISCO", content=""),
            ]
        else:
            fields = [
                BaseFieldInfo(
                    w=0,
                    description="CONTINUAÇÃO INFORMAÇÕES COMPLEMENTARES",
                    content=additional_data,
                ),
            ]
        block_adic.add_fields([fields])
        block_adic.render()

        add_data_field = block_adic.fields[0]
        add_data_lines = add_data_field.get_content_lines()
        max_add_data_lines = add_data_field.get_max_content_lines()
        return add_data_lines, max_add_data_lines

    def _get_dest_end_text(self, ender):
        logradouro = extract_text(ender, "xLgr")
        numero = extract_text(ender, "nro")
        complemento = extract_text(ender, "xCpl")
        partes = [logradouro, numero]
        if complemento:
            partes.append(complemento)
        dest_end = ", ".join(partes)
        cpl_truncado = False
        if len(dest_end) > 85:
            dest_end = dest_end[:85]
            cpl_truncado = True
        return dest_end, complemento, cpl_truncado
